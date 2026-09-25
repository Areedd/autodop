"""Rebuild the frozen post-hoc positive subset; optionally run its serial small test."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from generate_composable_branch_suite import compile_sql, from_dict

SOURCE = ROOT / 'runs/preference_random200_20260911'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='New directory; existing outputs are never overwritten')
    parser.add_argument('--run', action='store_true', help='Execute 90 queries, eight configurations, one warmup and three measured rounds, serially')
    args = parser.parse_args()
    protocol = json.loads((SOURCE / 'PROTOCOL.json').read_text())
    manifest = json.loads((SOURCE / 'random200/positive90/manifest.json').read_text())
    banks = json.loads((SOURCE / 'banks.json').read_text())
    model = Path(protocol['model'])
    if hashlib.sha256(model.read_bytes()).hexdigest() != protocol['model_sha256']:
        raise ValueError('Frozen checkpoint hash mismatch')
    bodies = []
    if len(manifest['queries']) != 90:
        raise ValueError('Expected exactly 90 queries')
    for query in manifest['queries']:
        body = compile_sql(from_dict(query['tree']), banks[str(query['parameters']['start_month'])])
        if hashlib.sha256(body.encode()).hexdigest() != query['sql_sha256']:
            raise ValueError('Generated SQL hash mismatch: ' + query['id'])
        bodies.append(body)
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / 'sql').mkdir()
    for query, body in zip(manifest['queries'], bodies):
        path = out / 'sql' / (query['id'] + '.sql')
        path.write_text(body)
        query['sql_file'] = str(path)
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    selected = {q['id'] for q in manifest['queries']}
    vectors = json.loads((SOURCE / 'random200/vectors.json').read_text())
    vectors = {mode: {qid: v for qid, v in entries.items() if qid in selected} for mode, entries in vectors.items()}
    (out / 'vectors.json').write_text(json.dumps(vectors) + '\n')
    command = [sys.executable, str(ROOT / 'collect_paper_evaluations.py'),
        '--manifest', str(out / 'manifest.json'), '--trusted-collection', str(SOURCE / 'random200/trusted_merged.json'),
        '--output-dir', str(out / 'scored'), '--policy-model', str(model),
        '--decision-vectors', str(out / 'vectors.json'), '--modes', ','.join(protocol['modes']),
        '--scenario', 'serial', '--query-timeout-s', '120', '--auto-dop-max-dop', '16',
        '--torch-threads', '1', '--warmup', '1', '--repetitions', '3', '--seed', str(protocol['seed'] + 2)]
    (out / 'REPRODUCTION.json').write_text(json.dumps(dict(query_count=90, sql_hashes_verified=True,
        role='posthoc_positive_subset_not_independent_validation', command=command,
        source_manifest=str(SOURCE / 'random200/positive90/manifest.json')), indent=2) + '\n')
    print(f'Regenerated and hash-verified all 90 queries: {out}', flush=True)
    if args.run:
        sys.path.insert(0, str(SOURCE))
        from preflight import check
        check(out / 'PREFLIGHT.json')
        subprocess.run(command, check=True, cwd=ROOT.parents[1])
        subprocess.run([sys.executable, str(ROOT / 'scripts/verify_paper_evaluations.py'),
            '--collection-dir', str(out / 'scored'), '--output', str(out / 'scored/independent_verification.json')], check=True)

if __name__ == '__main__':
    main()
