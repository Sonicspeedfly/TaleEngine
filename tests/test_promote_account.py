"""scripts/promote_account.py: второй аккаунт становится основным (2.30.5)."""
import importlib.util
from pathlib import Path

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "promote_account", Path(__file__).resolve().parent.parent / "scripts" / "promote_account.py")
pa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa)

CFG = {
    "model_list": [
        {"model_name": "gemini-4-carbon", "litellm_params": {"model": "vertex_ai/gemini-4-carbon",
                                                             "vertex_location": "global"}},
        {"model_name": "imagen-4", "litellm_params": {"model": "vertex_ai/imagen-4.0-generate-001",
                                                      "vertex_location": "us-central1",
                                                      "vertex_project": "old-proj"}},
        {"model_name": "gpt", "litellm_params": {"model": "openai/gpt-x"}},
        {"model_name": "acc2/*", "litellm_params": {"model": "vertex_ai/*", "vertex_project": "new-proj",
                                                    "vertex_location": "global",
                                                    "vertex_credentials": "/root/acc2.json"}},
        {"model_name": "acc2/imagen-4", "litellm_params": {"model": "vertex_ai/imagen-4.0-generate-001"}},
        {"model_name": "acc2/carbon-priority", "litellm_params": {
            "model": "vertex_ai/gemini-4-carbon", "vertex_project": "new-proj",
            "extra_headers": {"X-Vertex-AI-LLM-Request-Type": "shared"}}},
    ],
    "litellm_settings": {"drop_params": True, "vertex_project": "old-proj"},
    "general_settings": {"master_key": "sk-x"},
}


def test_promote_moves_main_models_to_second_project():
    new, log = pa.promote(yaml.safe_load(yaml.safe_dump(CFG)), "acc2/")
    by = {m["model_name"]: m["litellm_params"] for m in new["model_list"]}
    assert set(by) == {"gemini-4-carbon", "imagen-4", "gpt", "carbon-priority"}
    assert by["gemini-4-carbon"]["vertex_project"] == "new-proj"
    assert by["gemini-4-carbon"]["vertex_credentials"] == "/root/acc2.json"
    # Регион каждой модели — свой; чужие провайдеры не трогаются.
    assert by["imagen-4"]["vertex_location"] == "us-central1" and by["imagen-4"]["vertex_project"] == "new-proj"
    assert by["gpt"] == {"model": "openai/gpt-x"}
    # Псевдоним без пары без префикса не теряется.
    assert by["carbon-priority"]["extra_headers"]["X-Vertex-AI-LLM-Request-Type"] == "shared"
    assert new["litellm_settings"]["vertex_project"] == "new-proj"
    assert new["litellm_settings"]["vertex_credentials"] == "/root/acc2.json"
    assert new["general_settings"]["master_key"] == "sk-x"
    assert not any(m["model_name"].startswith("acc2/") for m in new["model_list"])


def test_promote_writes_backup(tmp_path, capsys):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(CFG, allow_unicode=True), encoding="utf-8")
    assert pa.main(["x", str(p), "acc2"]) == 0
    assert list(tmp_path.glob("config.yaml.bak-*"))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["model_list"][0]["litellm_params"]["vertex_project"] == "new-proj"


def test_promote_without_account_entries_refuses():
    with pytest.raises(SystemExit):
        pa.promote({"model_list": [{"model_name": "a", "litellm_params": {"model": "vertex_ai/a"}}]}, "acc2/")
