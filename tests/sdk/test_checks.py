import json

import pytest

from gate.contract import static_gates
from radiant_sdk import reasons as R
from radiant_sdk.checks import check_lock, code_sha256, run_static_checks
from radiant_sdk.packaging import prepare

from .conftest import reference_files, zip_of


def code_of(fn, *args):
    with pytest.raises(R.SolutionRejected) as e:
        fn(*args)
    return e.value.code


def test_reference_miner_passes_static_checks(miner_dir, manifest, tmp_path):
    p = prepare(miner_dir, tmp_path / "build")
    report = run_static_checks((tmp_path / "build" / "solution.zip").read_bytes(), manifest)
    assert report.sha256 == p.sha256 and report.code_sha256 == p.code_sha256
    assert report.descriptor["gpu"] == "T4"
    assert report.env["HF_REPO_ID"] == "tripathiarpan20/densenet121-res224-all-safetensors" and report.env["HF_TOKEN"] == ""
    assert {"requirements.in", ".env.template", "miner.env.template"} <= set(p.skipped)


def test_prepare_is_deterministic(miner_dir, tmp_path):
    a = prepare(miner_dir, tmp_path / "a")
    (miner_dir / "model.py").touch()  # new mtime, same bytes
    b = prepare(miner_dir, tmp_path / "b")
    assert (tmp_path / "a" / "solution.zip").read_bytes() == (tmp_path / "b" / "solution.zip").read_bytes()
    assert a.sha256 == b.sha256


def test_code_sha256_ignores_env_only(miner_dir):
    files = reference_files(miner_dir)
    other = {**files, ".env": b"HF_REPO_ID=a/b\nHF_REVISION=" + b"0" * 40 + b"\nHF_TOKEN=hf_secret\n"}
    assert code_sha256(files) == code_sha256(other)
    assert code_sha256(files) != code_sha256({**files, "model.py": files["model.py"] + b"\n"})


def test_missing_override_gets_the_same_code_from_sdk_and_validator(miner_dir, manifest):
    files = reference_files(miner_dir)
    src = files["model.py"].decode()
    start = src.index("    def map_to_labels")
    files["model.py"] = src[:start].encode()
    data = zip_of(files)
    sdk_code = code_of(run_static_checks, data, manifest)
    validator_code, _ = static_gates(data, manifest)
    assert sdk_code == validator_code == R.CONTRACT_MISSING_OVERRIDE


@pytest.mark.parametrize("model_src,code", [
    ("import socket\nfrom radiant_sdk.models.v1 import ChestXRayClassifier\n", R.BANNED_IMPORT),
    ("from urllib.request import urlopen\n", R.BANNED_IMPORT),
    ("import importlib\nx = importlib.import_module('subprocess')\n", R.BANNED_IMPORT),
    ("x = __import__('ctypes')\n", R.BANNED_IMPORT),
    ("def broken(:\n", R.SYNTAX_ERROR),
    ("class A:\n    pass\n", R.CONTRACT_NO_SUBCLASS),
])
def test_contract_rejections(miner_dir, manifest, model_src, code):
    files = {**reference_files(miner_dir), "model.py": model_src.encode()}
    assert code_of(run_static_checks, zip_of(files), manifest) == code


def test_two_subclasses_and_module_alias(miner_dir, manifest):
    methods = "".join(f"    def {m}(self, *a): pass\n" for m in
                      ("load_model", "load_model_checkpoint", "run_single_evaluation", "run_batched_evaluation",
                       "map_to_labels"))
    two = ("import radiant_sdk.models.v1 as v1\n"
           f"class A(v1.ChestXRayClassifier):\n{methods}class B(v1.ChestXRayClassifier):\n{methods}")
    files = {**reference_files(miner_dir), "model.py": two.encode()}
    assert code_of(run_static_checks, zip_of(files), manifest) == R.CONTRACT_MULTIPLE_SUBCLASSES
    one = f"import radiant_sdk.models.v1 as v1\nclass A(v1.ChestXRayClassifier):\n{methods}"
    run_static_checks(zip_of({**files, "model.py": one.encode()}), manifest)


def test_banned_import_in_src_module(miner_dir, manifest):
    files = {**reference_files(miner_dir), "src/helpers.py": b"import subprocess\n"}
    assert code_of(run_static_checks, zip_of(files), manifest) == R.BANNED_IMPORT


@pytest.mark.parametrize("change,code", [
    ({"gpu": "H100"}, R.GPU_NOT_ALLOWED),
    ({"gpu": "L4"}, R.GPU_NOT_ALLOWED),
    ({"sdk_version": "2.0.0"}, R.SDK_VERSION_MISMATCH),
    ({"base_class": "radiant_sdk.models.v2:ChestXRayClassifier"}, R.BASE_CLASS_MISMATCH),
    ({"cpus": 64}, R.CPUS_OVER_LIMIT),
    ({"batch_size": 0}, R.BATCH_SIZE_INVALID),
    ({"extra": 1}, R.DESCRIPTOR_INVALID),
])
def test_descriptor_rejections(miner_dir, manifest, change, code):
    files = reference_files(miner_dir)
    d = {**json.loads(files["radiant_solution.json"]), **change}
    files["radiant_solution.json"] = json.dumps(d).encode()
    assert code_of(run_static_checks, zip_of(files), manifest) == code


@pytest.mark.parametrize("env,code", [
    ("HF_REPO_ID=a/b\nHF_REVISION=" + "a" * 40 + "\nRADIANT_API_URL=x\n", R.ENV_UNKNOWN_KEY),
    ("HF_REPO_ID=a/b\nHF_REVISION=main\n", R.ENV_BAD_REVISION),
    ("HF_REPO_ID=justname\nHF_REVISION=" + "a" * 40 + "\n", R.ENV_BAD_REPO),
    ("HF_REPO_ID=a/b\nHF_REPO_ID=c/d\nHF_REVISION=" + "a" * 40 + "\n", R.ENV_INVALID),
])
def test_env_rejections(miner_dir, manifest, env, code):
    files = {**reference_files(miner_dir), ".env": env.encode()}
    assert code_of(run_static_checks, zip_of(files), manifest) == code


HASH = "--hash=sha256:" + "a" * 64


@pytest.mark.parametrize("lock,code", [
    ("numpy>=1.0 " + HASH, R.LOCK_UNPINNED),
    ("numpy==2.2.6", R.LOCK_MISSING_HASH),
    ("--index-url https://evil.example/simple\nnumpy==2.2.6 " + HASH, R.LOCK_FORBIDDEN_OPTION),
    ("-e ./src", R.LOCK_FORBIDDEN_OPTION),
    ("evil @ https://x/evil.whl " + HASH, R.LOCK_FORBIDDEN_OPTION),
    ("-r other.txt", R.LOCK_FORBIDDEN_OPTION),
])
def test_lock_rejections(lock, code):
    assert code_of(check_lock, lock) == code


def test_lock_accepts_uv_output_with_markers_and_continuations():
    text = (f"numpy==2.2.6 \\\n    {HASH} \\\n    {HASH}\n    # via foo\n"
            f"tzdata==2026.6 ; sys_platform == 'win32' \\\n    {HASH}\n")
    assert check_lock(text) == ["numpy==2.2.6", "tzdata==2026.6"]
    assert check_lock("") == []


@pytest.mark.parametrize("extra,code", [
    ({"../evil.py": b"x"}, R.PATH_ESCAPE),
    ({"/abs.py": b"x"}, R.PATH_ESCAPE),
    ({"src/weights.pt": b"\x80\x02binary"}, R.OPAQUE_BINARY),
    ({"src/inner.zip": b"PK\x03\x04rest"}, R.NESTED_ARCHIVE),
    ({"notes.txt": b"hi"}, R.UNEXPECTED_FILE),
    ({"src/model.bin": b"text but wrong suffix"}, R.UNEXPECTED_FILE),
])
def test_archive_rejections(miner_dir, manifest, extra, code):
    files = {**reference_files(miner_dir), **extra}
    assert code_of(run_static_checks, zip_of(files), manifest) == code


def test_symlink_missing_file_and_size_caps(miner_dir, manifest):
    files = reference_files(miner_dir)
    assert code_of(run_static_checks, zip_of(files, symlink="src/link.py"), manifest) == R.SYMLINK
    no_lock = {k: v for k, v in files.items() if k != "requirements.lock"}
    assert code_of(run_static_checks, zip_of(no_lock), manifest) == R.MISSING_FILE
    bomb = {**files, "src/big.txt": b"a" * 51_000_000}  # compresses tiny, inflates past 50 MB
    assert code_of(run_static_checks, zip_of(bomb), manifest) == R.UNZIPPED_TOO_LARGE
    small = {**manifest, "limits": {**manifest["limits"], "max_solution_zip_mb": 1}}
    import os
    noisy = {**files, "src/noise.txt": os.urandom(1_500_000).hex().encode()}
    assert code_of(run_static_checks, zip_of(noisy), small) == R.ZIP_TOO_LARGE
    assert code_of(run_static_checks, b"not a zip", manifest) == R.ZIP_INVALID


def test_weights_repo_must_be_safetensors_only():
    from radiant_sdk.checks import check_weight_files

    check_weight_files(["model.safetensors", "config.json", "README.md", ".gitattributes"], ["safetensors"])
    for bad in (["model.pt", "config.json"], ["model.safetensors", "extra.bin"], ["weights.pkl"],
                ["config.json", "README.md"]):
        assert code_of(check_weight_files, bad, ["safetensors"]) == R.HF_WEIGHTS_FORMAT


def test_manifest_pins_weights_formats(manifest):
    assert manifest["gates"]["weights_formats"] == ["safetensors"]
