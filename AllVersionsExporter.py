import adsk.core
import traceback
from pathlib import Path
from datetime import datetime
from typing import NamedTuple, List, Set
from enum import Enum
from dataclasses import dataclass
import hashlib
import re

log_file = None
log_fh = None

handlers = []

def log(*args):
    print(*args, file=log_fh)
    log_fh.flush()

def init_directory(name):
    directory = Path(name)
    directory.mkdir(exist_ok=True)
    return directory

def init_logging(directory):
    global log_file, log_fh
    log_file = directory / '{:%Y_%m_%d_%H_%M}.txt'.format(datetime.now())
    log_fh = open(log_file, 'w')

class Format(Enum):
    F3D = 'f3d'
    STEP = 'step'
    STL = 'stl'
    IGES = 'igs'
    SAT = 'sat'
    SMT = 'smt'

FormatFromName = {x.value: x for x in Format}

DEFAULT_SELECTED_FORMATS = {Format.F3D, Format.STEP}

class Ctx(NamedTuple):
    folder: Path
    formats: List[Format]
    app: adsk.core.Application
    projects: Set[str]
    unhide_all: bool
    save_sketches: bool
    save_all_versions: bool

    def extend(self, other):
        return self._replace(folder=self.folder / other)

class LazyDocument:
    def __init__(self, ctx, file):
        self._ctx = ctx
        self._file = file
        self._document = None

    def open(self):
        if self._document is not None:
            return
        log(f'Opening `{self._file.name}`')
        self._document = self._ctx.app.documents.open(self._file)
        self._document.activate()

        if self._ctx.unhide_all:
            unhide_all_in_document(self._document)

    def close(self):
        if self._document is None:
            return
        log(f'Closing {self._file.name}')
        self._document.close(False)  # don't save changes

    @property
    def design(self):
        return design_from_document(self._document)

    @property
    def rootComponent(self):
        return self.design.rootComponent

@dataclass
class Counter:
    saved: int = 0
    skipped: int = 0
    errored: int = 0

    def __add__(self, other):
        return Counter(
            self.saved + other.saved,
            self.skipped + other.skipped,
            self.errored + other.errored,
        )
    def __iadd__(self, other):
        self.saved += other.saved
        self.skipped += other.skipped
        self.errored += other.errored
        return self

def design_from_document(document: adsk.core.Document):
    return adsk.fusion.FusionDocument.cast(document).design

def unhide_all_in_document(document: adsk.core.Document):
    unhide_all_in_component(design_from_document(document).rootComponent)

def unhide_all_in_component(component):
    component.isBodiesFolderLightBulbOn = True
    component.isSketchFolderLightBulbOn = True

    for brep in component.bRepBodies:
        brep.isLightBulbOn = True

    for body in component.meshBodies:
        body.isLightBulbOn = True

    # I find the name occurrences very confusing, but apparently that is what a sub-component is called
    for occurrence in component.occurrences:
        occurrence.isLightBulbOn = True
        unhide_all_in_component(occurrence.component)

def sanitize_filename(name: str) -> str:
    """
    Remove "bad" characters from a filename. Right now just punctuation that Windows doesn't like
    If any chars are removed, we append _{hash} so that we don't accidentally clobber other files
    since eg `Model 1/2` and `Model 1 2` would otherwise have the same name
    """
    # this list of characters is just from trying to rename a file in Explorer (on Windows)
    # I think the actual requirements are per fileystem and will be different on Mac
    # I'm not sure how other unicode chars are handled
    with_replacement = re.sub(r'[:\\/*?<>|]', ' ', name)
    if name == with_replacement:
        return name
    log(f'filename `{name}` contained bad chars, replacing by `{with_replacement}`')
    hash = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f'{with_replacement}_{hash}'

def export_filename(ctx: Ctx, format: Format, file):
    sanitized = sanitize_filename(file.name)
    name = f'{sanitized}_v{file.versionNumber}.{format.value}'
    return ctx.folder / sanitized / name

def export_sketches(ctx, component):
    counter = Counter()
    for sketch in component.sketches:
        output_path = ctx.folder / f'{sanitize_filename(sketch.name)}.dxf'
        if output_path.exists():
            log(f'{output_path} already exists, skipping')
            counter.skipped += 1
        else:
            log(f'Exporting sketch {sketch.name} in {component.name} to {output_path}')
            try:
                output_path.parent.mkdir(exist_ok=True, parents=True)
                sketch.saveAsDXF(str(output_path))
                counter.saved += 1
            except Exception:
                log(traceback.format_exc())
                counter.errored += 1

    for occurrence in component.occurrences:
        counter += export_sketches(ctx.extend(sanitize_filename(occurrence.name)), occurrence.component)

    return counter

def export_file(ctx: Ctx, format: Format, file, doc: LazyDocument) -> Counter:
    # TODO コメントも保存する
    output_path = export_filename(ctx, format, file)
    if output_path.exists():
        log(f'{output_path} already exists, skipping')
        return Counter(skipped=1)

    doc.open()

    # I'm just taking this from here https://github.com/tapnair/apper/blob/master/apper/Fusion360Utilities.py
    # is there a nicer way to do this??
    design = doc.design
    em = design.exportManager

    output_path.parent.mkdir(exist_ok=True, parents=True)

    # leaving this ugly, not sure what else there might be to handle per format
    if format == Format.F3D:
        options = em.createFusionArchiveExportOptions(str(output_path))
    elif format == Format.STL:
        options = em.createSTLExportOptions(design.rootComponent, str(output_path))
    elif format == Format.STEP:
        options = em.createSTEPExportOptions(str(output_path))
    elif format == Format.IGES:
        options = em.createIGESExportOptions(str(output_path))
    elif format == Format.SAT:
        options = em.createSATExportOptions(str(output_path))
    elif format == Format.SMT:
        options = em.createSMTExportOptions(str(output_path))
    else:
        raise Exception(f'Got unknown export format {format}')

    em.execute(options)
    log(f'Saved {output_path}')

    return Counter(saved=1)

def export_metadata(ctx: Ctx, file: adsk.core.DataFile) ->Counter:
    counter = Counter()
    sanitized = sanitize_filename(file.name)
    name = f'{sanitized}_metadata.txt'
    output_path = ctx.folder / sanitized / name
    if not output_path.exists():
        counter = Counter(saved=1)
        with open(output_path, 'w'):
            pass
        log(f'Created {output_path}')

    with open(output_path, 'r') as f:
        lines = f.readlines()
        lines_strip = [line.strip() for line in lines]
        if f'Version: {file.versionNumber}'.strip() in lines_strip:
            return counter

    with open(output_path, 'a+') as f:
        f.write("\n".join((
            f'Version: {file.versionNumber}',
            f'\tcreated: {datetime.fromtimestamp(file.dateCreated)}',
            f'\tdescription: {file.description}\n',
        )))
        f.flush()
        log(f'Updated {output_path}')
    return counter

def visit_file(ctx: Ctx, file: adsk.core.DataFile) -> Counter:
    log(f'Visiting file {file.name} v{file.versionNumber} . {file.fileExtension}')

    if file.fileExtension != 'f3d':
        log(f'file {file.name} has extension {file.fileExtension} which is not currently handled, skipping')
        return Counter(skipped=1)

    doc = LazyDocument(ctx, file)

    counter = Counter()

    if ctx.save_sketches:
        doc.open()
        counter += export_sketches(ctx.extend(sanitize_filename(doc.rootComponent.name)), doc.rootComponent)

    for format in ctx.formats:
        try:
            counter += export_file(ctx, format, file, doc)
        except Exception:
            counter.errored += 1
            log(traceback.format_exc())

    try:
        counter += export_metadata(ctx, file)
    except Exception:
        counter.errored += 1
        log(traceback.format_exc())

    doc.close()
    return counter

def visit_file_wrapper(ctx: Ctx, file: adsk.core.DataFile) -> Counter:
    log(f'Visiting file {file.name}.{file.fileExtension}, which has {file.versionNumber} versions.')

    if(ctx.save_all_versions):
        counter = Counter()
        for specificVersionFile in file.versions.asArray():
            counter += visit_file(ctx, specificVersionFile)
        return counter
    else:
        return visit_file(ctx, file)



def visit_folder(ctx: Ctx, folder) -> Counter:
    log(f'Visiting folder {folder.name}')

    new_ctx = ctx.extend(sanitize_filename(folder.name))

    counter = Counter()

    for file in folder.dataFiles:
        try:
            counter += visit_file_wrapper(new_ctx, file)
        except Exception:
            log(f'Got exception visiting file\n{traceback.format_exc()}')
            counter.errored += 1

    for sub_folder in folder.dataFolders:
        counter += visit_folder(new_ctx, sub_folder)

    return counter

def main(ctx: Ctx) -> Counter:
    init_directory(ctx.folder)
    init_logging(ctx.folder)

    counter = Counter()

    for project in ctx.app.data.dataProjects:
        if project.name in ctx.projects:
            counter += visit_folder(ctx, project.rootFolder)

    return counter

class ExporterCommandCreatedEventHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command

            cmd.setDialogInitialSize(600, 400)
            # http://help.autodesk.com/view/fusion360/ENU/?guid=GUID-C1BF7FBF-6D35-4490-984B-11EB26232EAD
            cmd.isExecutedWhenPreEmpted = False

            onExecute = ExporterCommandExecuteHandler()
            cmd.execute.add(onExecute)
            onDestroy = ExporterCommandDestroyHandler()
            cmd.destroy.add(onDestroy)
            handlers.append(onExecute)
            handlers.append(onDestroy)

            inputs = cmd.commandInputs

            inputs.addStringValueInput('directory', 'Directory', str(Path.home() / 'Desktop/Fusion360Export'))

            drop = inputs.addDropDownCommandInput('file_types', 'Export Types', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            for format in Format:
                drop.listItems.add(format.value, format in DEFAULT_SELECTED_FORMATS)

            drop = inputs.addDropDownCommandInput('projects', 'Export Projects', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            for project in adsk.core.Application.get().data.dataProjects:
                drop.listItems.add(project.name, True)

            inputs.addBoolValueInput('unhide_all', 'Unhide All Bodies', True, '', True)
            inputs.addBoolValueInput('save_sketches', 'Save Sketches as DXF', True, '', False)
            inputs.addBoolValueInput('save_all_versions', 'Save all versions', True, '', True)
        except:
            adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())

class ExporterCommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            adsk.terminate()
        except:
            adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())

# Dont use yield and don't copy list items, swig wants to delete things
def selected(inputs):
    return [it.name for it in inputs if it.isSelected]

class ExporterCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            start_dt = datetime.now()
            inputs = args.command.commandInputs

            app = adsk.core.Application.get()
            ui = app.userInterface

            ctx = Ctx(
                app = app,
                folder = Path(inputs.itemById('directory').value),
                formats = [FormatFromName[x] for x in selected(inputs.itemById('file_types').listItems)],
                projects = set(selected(inputs.itemById('projects').listItems)),
                unhide_all = inputs.itemById('unhide_all').value,
                save_sketches = inputs.itemById('save_sketches').value,
                save_all_versions= inputs.itemById('save_all_versions').value,
            )

            counter = main(ctx)
            end_dt = datetime.now()
            result = '\n'.join((
                f'Saved {counter.saved} files',
                f'Skipped {counter.skipped} files',
                f'Encountered {counter.errored} errors',
                f'Log file is at {log_file}',
                f'Elapsed time: {end_dt - start_dt}'))
            log("\n\n========Result========")
            log(result)
            ui.messageBox(result)

        except:
            tb = traceback.format_exc()
            adsk.core.Application.get().userInterface.messageBox(f'Log file is at {log_file}\n{tb}')
            if log_fh is not None:
                log(f'Got top level exception\n{tb}')
        finally:
            if log_fh is not None:
                log_fh.close()

def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        cmd_defs = ui.commandDefinitions

        CMD_DEF_ID = 'nPeeech_AllVersionExporter'
        cmd_def = cmd_defs.itemById(CMD_DEF_ID)
        # This isn't how all the other demo scripts manage the lifecycle, but if we don't delete the old
        # command then we get double inputs when we run a second time
        if cmd_def:
            cmd_def.deleteMe()

        cmd_def = cmd_defs.addButtonDefinition(
            CMD_DEF_ID,
            'Export all the things',
            'Tooltip',
        )

        cmd_created = ExporterCommandCreatedEventHandler()
        cmd_def.commandCreated.add(cmd_created)
        handlers.append(cmd_created)

        cmd_def.execute()

        adsk.autoTerminate(False)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))