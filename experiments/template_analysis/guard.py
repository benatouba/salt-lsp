"""Run the CLI with tripwires; a forbidden attempt fails, not just its result."""

from pathlib import Path
import runpy
import sys

import jinja2


def forbidden(*args, **kwargs):
    raise AssertionError("rendering or template loading attempted")


jinja2.Template.render = forbidden
jinja2.Template.generate = forbidden
jinja2.Template.render_async = forbidden
jinja2.Template.generate_async = forbidden
jinja2.Environment.compile = forbidden
jinja2.Environment.get_template = forbidden
jinja2.Environment.from_string = forbidden


def audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        raise AssertionError("external execution attempted")
    if event == "import" and args[0].split(".")[0] in {
        "salt",
        "workspace_probe",
    }:
        raise AssertionError("workspace/Salt import attempted")
    if event == "exec" and "/fixtures/sentinel/" in args[0].co_filename:
        raise AssertionError("project code execution attempted")


sys.addaudithook(audit)
runpy.run_path(str(Path(__file__).with_name("proof.py")), run_name="__main__")
