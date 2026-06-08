from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_ltr_train_data_scarcity_is_scheduler_success(monkeypatch, capsys):
    ltr_train = _load("ltr_train", "cli/ltr_train.py")

    class FakeSecretFile:
        def exists(self) -> bool:
            return True

        def read_text(self) -> str:
            return "token"

    monkeypatch.setattr(ltr_train, "SECRET_FILE", FakeSecretFile())
    monkeypatch.setattr(ltr_train, "collect_training_data", lambda token: ([], []))
    monkeypatch.setattr(sys, "argv", ["ltr_train.py"])

    assert ltr_train.main() == 0
    assert '"status": "insufficient_samples"' in capsys.readouterr().out


def test_brain_finetune_disabled_is_scheduler_success(monkeypatch, capsys):
    brain_finetune = _load("brain_finetune", "cli/brain_finetune.py")

    monkeypatch.setenv("BRAIN_FINETUNE_ENABLED", "false")
    monkeypatch.setattr(sys, "argv", ["brain_finetune.py"])

    assert brain_finetune.main() == 0
    assert '"status": "disabled"' in capsys.readouterr().out


def test_flagged_heavy_recurring_jobs_start_off_hours():
    job_definitions = _load("job_definitions", "brain_core/job_definitions.py")
    names = {"ltr_train", "embed_finetune", "lora_ab_gate", "raptor_build"}

    jobs = {job.name: job for job in job_definitions.JOB_SCHEDULE if job.name in names}

    assert set(jobs) == names
    assert jobs["ltr_train"].trigger.fields[5].expressions[0].first == 4
    assert jobs["embed_finetune"].trigger.fields[5].expressions[0].first == 23
    assert jobs["lora_ab_gate"].trigger.fields[5].expressions[0].first == 1
    assert jobs["raptor_build"].trigger.fields[5].expressions[0].first == 7
