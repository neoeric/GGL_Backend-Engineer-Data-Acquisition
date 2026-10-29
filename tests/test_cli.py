import json
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
URL="https://www.threads.com/@fixture_author/post/ROOT"


def cli(tmp_path,*extra):
    env=dict(os.environ,PYTHONPATH=str(ROOT/"src"),PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable,"-m","threads_agent","--message","post_text "+URL,
        "--model","mock","--fixture",str(ROOT/"tests/fixtures/thread.json"),*extra],
        cwd=tmp_path,env=env,capture_output=True,text=True,encoding="utf-8",timeout=15)


def test_cli_stdout_and_trace(tmp_path):
    p=cli(tmp_path,"--output","saved.json")
    assert p.returncode==0 and p.stderr==""
    result=json.loads(p.stdout)
    assert result==json.loads((tmp_path/"saved.json").read_text(encoding="utf-8"))
    trace=json.loads((tmp_path/"trace.json").read_text(encoding="utf-8"))
    assert result["run_id"]==trace["run_id"]


def test_unwritable_output_is_nonzero(tmp_path):
    blocked=tmp_path/"blocked";blocked.write_text("file",encoding="utf-8")
    p=cli(tmp_path,"--output",str(blocked/"result.json"))
    assert p.returncode==1 and "output_write_error" in p.stderr
    result=json.loads(p.stdout)
    assert "output_write_error" in {i["reason_code"] for i in result["issues"]}
    trace=json.loads((tmp_path/"trace.json").read_text(encoding="utf-8"))
    assert result["status"]==trace["status"]==trace["events"][-1]["status"]
    assert result["issues"]==trace["events"][-1]["issues"]
