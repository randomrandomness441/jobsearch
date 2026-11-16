"""Run every test file. `python3 run_tests.py` runs everything; `--fast` skips
test_pipeline.py (it renders real PDFs with LibreOffice and takes ~40s)."""
import shutil
import subprocess
import sys

PY_TESTS = ["test_ats", "test_fetch", "test_db", "test_master", "test_worker", "test_server", "test_pipeline"]


def main():
    fast = "--fast" in sys.argv
    failed = []
    for name in PY_TESTS:
        if fast and name == "test_pipeline":
            continue
        print(f"== {name}", flush=True)
        if subprocess.run([sys.executable, f"{name}.py"]).returncode:
            failed.append(name)
    if shutil.which("node"):
        print("== test_jobparse.js", flush=True)
        if subprocess.run(["node", "test_jobparse.js"]).returncode:
            failed.append("test_jobparse.js")
    print("\nFAILED: " + ", ".join(failed) if failed else "\nall test files passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
