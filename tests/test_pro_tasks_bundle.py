from __future__ import annotations

import json
from pathlib import Path

import pytest

from controller.atomic import atomic_write_json, atomic_write_text
from controller.eval_bundle import (
    EXCLUSION_REASONS,
    prepare_eval_bundle,
    recompute_bundle_digest,
    record_conformance,
    validate_bundle_conformance,
)
from controller.pro_tasks import (
    DatasetMetadata,
    TaskLoadError,
    load_fixture_rows,
    materialize_task_list,
    recompute_task_list_hash,
)


FIXTURE = Path(__file__).parent / "fixtures" / "pro_tasks" / "pro_fixture.jsonl"
MALFORMED = (
    Path(__file__).parent / "fixtures" / "pro_tasks" / "malformed_missing_base.jsonl"
)


def _materialize_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    output = tmp_path / "tasks.jsonl"
    manifest_path = tmp_path / "tasks.manifest.json"
    manifest = materialize_task_list(
        load_fixture_rows(FIXTURE),
        output_path=output,
        manifest_path=manifest_path,
        dataset=DatasetMetadata(expected_row_count=None),
    )
    return output, manifest_path, manifest


def test_task_list_materialization_is_deterministic_and_hashable(
    tmp_path: Path,
) -> None:
    output_a, _, manifest_a = _materialize_fixture(tmp_path / "a")
    output_b, _, manifest_b = _materialize_fixture(tmp_path / "b")

    assert output_a.read_bytes() == output_b.read_bytes()
    assert manifest_a["dataset"]["row_count"] == 6
    assert manifest_a["dataset"]["content_hash"] == recompute_task_list_hash(output_a)
    assert (
        manifest_a["dataset"]["content_hash"] == manifest_b["dataset"]["content_hash"]
    )
    first = json.loads(output_a.read_text(encoding="utf-8").splitlines()[0])
    assert {
        "instance_id",
        "repo",
        "base_commit",
        "fail_to_pass",
        "pass_to_pass",
    }.issubset(first)


def test_malformed_dataset_row_fails_without_partial_output(tmp_path: Path) -> None:
    output = tmp_path / "bad.jsonl"
    with pytest.raises(TaskLoadError, match="base_commit"):
        materialize_task_list(
            load_fixture_rows(MALFORMED),
            output_path=output,
            dataset=DatasetMetadata(expected_row_count=None),
        )
    assert not output.exists()


def _write_normalized(
    run_root: Path,
    attempt_id: str,
    payload: dict,
    patch: str
    | None = "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n",
) -> None:
    attempt_dir = run_root / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        attempt_dir / "normalized.json", {"attempt_id": attempt_id, **payload}
    )
    if patch is not None:
        atomic_write_text(attempt_dir / "patch.diff", patch)


def test_eval_bundle_closed_set_manifest_and_integrity(tmp_path: Path) -> None:
    task_list, task_manifest, _ = _materialize_fixture(tmp_path / "tasks")
    run_root = tmp_path / "run"
    _write_normalized(run_root, "repo_a__task-001", {"queued_for_evaluation": True})
    _write_normalized(
        run_root,
        "repo_a__task-002",
        {"queued_for_evaluation": False, "precheck_failed": True},
    )
    _write_normalized(
        run_root,
        "repo_b__task-003",
        {"queued_for_evaluation": False, "no_patch": True},
        patch="",
    )
    (run_root / "attempts" / "repo_c__task-004").mkdir(parents=True)
    _write_normalized(
        run_root,
        "repo_e__task-006",
        {"queued_for_evaluation": True},
        patch=None,
    )

    manifest = prepare_eval_bundle(
        run_root=run_root,
        task_list_path=task_list,
        task_manifest_path=task_manifest,
        output_dir=run_root / "eval-bundle",
        run_id="bundle-test",
    )

    assert manifest["included_count"] == 1
    assert manifest["exclusion_reasons_closed_set"] == list(EXCLUSION_REASONS)
    assert manifest["exclusion_counts"] == {
        "precheck_failed": 1,
        "no_patch": 1,
        "attempt_incomplete": 1,
        "attempt_missing": 1,
        "artifact_inconsistent": 1,
    }
    assert (
        "headline denominator remains the full 731"
        in manifest["bundle_candidate_denominator_note"]
    )
    assert manifest["dataset"]["content_hash"]
    assert manifest["upstream_evaluator_reference"]["commit"]
    assert (
        recompute_bundle_digest(run_root / "eval-bundle", manifest)
        == manifest["integrity"]["bundle_digest"]
    )
    assert set(manifest["per_repo_breakdown"]["example/repo_a"]) == {
        "considered",
        "included",
        "excluded",
        "excluded_by_reason",
    }


def test_eval_bundle_conformance_can_be_recorded(tmp_path: Path) -> None:
    task_list, task_manifest, _ = _materialize_fixture(tmp_path / "tasks")
    run_root = tmp_path / "run"
    _write_normalized(run_root, "repo_a__task-001", {"queued_for_evaluation": True})
    output_dir = run_root / "eval-bundle"
    prepare_eval_bundle(
        run_root=run_root,
        task_list_path=task_list,
        task_manifest_path=task_manifest,
        output_dir=output_dir,
        run_id="bundle-conformance",
    )

    result = validate_bundle_conformance(bundle_dir=output_dir)
    manifest = record_conformance(output_dir, result)

    assert result["status"] == "passed"
    assert manifest["conformance"]["status"] == "passed"


def test_eval_bundle_conformance_runs_upstream_evaluator_loader_probe(
    tmp_path: Path,
) -> None:
    task_list, task_manifest, _ = _materialize_fixture(tmp_path / "tasks")
    run_root = tmp_path / "run"
    _write_normalized(run_root, "repo_a__task-001", {"queued_for_evaluation": True})
    output_dir = run_root / "eval-bundle"
    prepare_eval_bundle(
        run_root=run_root,
        task_list_path=task_list,
        task_manifest_path=task_manifest,
        output_dir=output_dir,
        run_id="bundle-upstream-probe",
    )
    upstream = _fake_upstream_checkout(tmp_path / "upstream")

    result = validate_bundle_conformance(
        bundle_dir=output_dir, upstream_checkout=upstream, require_upstream=True
    )

    assert result["status"] == "passed"
    assert result["mode"] == "upstream-evaluator-schema-probe"
    assert result["upstream_probe"]["status"] == "passed"
    assert result["upstream_probe"]["result_count"] == 1


def _fake_upstream_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text(
        "patches.json rows contain instance_id, patch, prefix\n", encoding="utf-8"
    )
    (path / "swe_bench_pro_eval.py").write_text(
        "import argparse, concurrent.futures, json, os\n"
        "import pandas as pd\n"
        "from tqdm import tqdm\n"
        "def eval_with_modal(*a, **k): raise RuntimeError('probe should replace this')\n"
        "def parse_args():\n"
        "    p=argparse.ArgumentParser()\n"
        "    p.add_argument('--raw_sample_path', required=True)\n"
        "    p.add_argument('--patch_path', required=True)\n"
        "    p.add_argument('--output_dir', required=True)\n"
        "    p.add_argument('--scripts_dir', required=True)\n"
        "    p.add_argument('--num_workers', type=int, default=1)\n"
        "    p.add_argument('--dockerhub_username', required=True)\n"
        "    return p.parse_args()\n"
        "def main():\n"
        "    args=parse_args()\n"
        "    raw_sample_df=pd.read_json(args.raw_sample_path, lines=True).fillna('').set_index('instance_id', drop=False)\n"
        "    patches=json.load(open(args.patch_path))\n"
        "    eval_results={}\n"
        "    with concurrent.futures.ThreadPoolExecutor(max_workers=args.num_workers) as executor:\n"
        "        futures={executor.submit(eval_with_modal, p.get('patch',''), raw_sample_df.loc[p['instance_id']], args.output_dir, args.dockerhub_username, args.scripts_dir, prefix=p.get('prefix','')): p for p in patches if p['instance_id'] in raw_sample_df.index}\n"
        "        pbar=tqdm(concurrent.futures.as_completed(futures), total=len(futures))\n"
        "        for future in pbar:\n"
        "            patch=futures[future]\n"
        "            output=future.result()\n"
        "            raw=raw_sample_df.loc[patch['instance_id']]\n"
        "            passed={x['name'] for x in output['tests'] if x['status']=='PASSED'}\n"
        "            eval_results[patch['instance_id']]=set(eval(raw['fail_to_pass']) + eval(raw['pass_to_pass'])) <= passed\n"
        "            pbar.set_description('Accuracy: 100%')\n"
        "    os.makedirs(args.output_dir, exist_ok=True)\n"
        "    json.dump(eval_results, open(os.path.join(args.output_dir, 'eval_results.json'), 'w'))\n"
        "if __name__ == '__main__': main()\n",
        encoding="utf-8",
    )
    return path
