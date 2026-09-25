#!/usr/bin/env python3
"""Build offline Docker images from compiled FE, an audited BE image and a policy."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AUTODOP = ROOT / 'tools/autodop'
sys.path.insert(0, str(AUTODOP))
from autodop.training import file_sha256
from autodop.model import load_policy_model


def run(*command):
    subprocess.run(list(map(str, command)), check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory only')
    parser.add_argument('--policy-model', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--be-image', required=True, help='Existing BE image with matching source provenance')
    parser.add_argument('--be-build-proof', type=Path, required=True)
    args = parser.parse_args()
    policy = args.policy_model.resolve()
    load_policy_model(policy)  # Fail before creating/building a release.
    proof = json.loads(args.be_build_proof.read_text())
    for name, expected in proof['source_sha256'].items():
        marker = '/be/'
        if marker in name:
            source = ROOT / 'be' / name.split(marker, 1)[1]
            if file_sha256(source) != expected:
                raise ValueError(f'BE build provenance does not match {source}')
    base = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.be_image]))[0]
    # Check the actual image, not merely the historical build record.
    digest = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
        '--entrypoint', 'sha256sum', base['Id'], '/opt/starrocks/be/lib/starrocks_be'], text=True).split()[0]
    if digest != proof['binary_sha256']:
        raise ValueError('BE binary in image does not match build provenance')
    jar = ROOT / 'fe/fe-core/target/starrocks-fe.jar'
    if not jar.is_file():
        raise ValueError('Build FE first; fe/fe-core/target/starrocks-fe.jar is missing')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    context = out / 'build-context'
    context.mkdir()
    shutil.copytree(AUTODOP / 'autodop', context / 'autodop', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(AUTODOP / 'autodop_server.py', context)
    templates = Path(__file__).resolve().parent
    for name in ['Dockerfile.autodop', 'Dockerfile.starrocks', 'fe.conf', 'be.conf', 'smoke.py']:
        shutil.copy2(templates / name, context)
    (context / 'models').mkdir()
    shutil.copy2(policy, context / 'models/policy.pt')
    shutil.copy2(args.request, context / 'models/request.json')
    fe = context / 'fe'
    for name in ['bin', 'conf', 'lib', 'meta', 'log']:
        (fe / name).mkdir(parents=True)
    for pattern in ['*_fe.sh', 'show_fe_version.sh', 'common.sh']:
        for path in (ROOT / 'bin').glob(pattern):
            shutil.copy2(path, fe / 'bin')
    for name in ['udf_security.policy', 'hadoop_env.sh', 'core-site.xml', 'cluster_snapshot.yaml']:
        shutil.copy2(ROOT / 'conf' / name, fe / 'conf')
    shutil.copytree(ROOT / 'fe/fe-core/target/lib', fe / 'lib', dirs_exist_ok=True)
    shutil.copy2(jar, fe / 'lib/starrocks-fe.jar')
    extension = ROOT / 'java-extensions/hadoop-ext/target/starrocks-hadoop-ext.jar'
    shutil.copy2(extension, fe / 'lib')
    shutil.copytree(ROOT / 'webroot', fe / 'webroot')
    # Independent contexts prevent transferring the large FE libraries into the Python build.
    (context / '.dockerignore').write_text('fe/\n')
    run('docker', 'build', '--network=host', '-f', context / 'Dockerfile.autodop',
        '-t', 'autodop/service:20260925', context)
    (context / '.dockerignore').write_text('autodop/\nmodels/\n')
    run('docker', 'build', '--build-arg', 'BE_IMAGE=' + base['Id'], '-f', context / 'Dockerfile.starrocks',
        '-t', 'autodop/starrocks:20260925', context)
    images = ['autodop/service:20260925', 'autodop/starrocks:20260925']
    shutil.copy2(templates / 'compose.yaml', out)
    shutil.copy2(templates / 'smoke.py', out)
    shutil.copytree(context / 'models', out / 'models')
    shutil.copytree(AUTODOP / 'docs', out / 'docs')
    shutil.copy2(AUTODOP / 'README.md', out)
    shutil.copy2(ROOT / 'LICENSE.txt', out)
    shutil.copy2(ROOT / 'NOTICE.txt', out)
    shutil.copytree(ROOT / 'licenses-binary', out / 'licenses-binary')
    packages = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
        'autodop/service:20260925', 'python', '-m', 'pip', 'freeze'], text=True)
    (out / 'PYTHON_PACKAGES.txt').write_text(packages)
    manifest = {'status': 'built_not_yet_tested', 'platform': 'linux/amd64',
        'git_head_at_build': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'policy_sha256': file_sha256(policy), 'fe_jar_sha256': file_sha256(jar),
        'be_binary_sha256': digest, 'be_base_image_id': base['Id'],
        'images': {tag: json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]['Id'] for tag in images},
        'source_sha256': {str(p.relative_to(ROOT)): file_sha256(p)
            for directory, pattern in [(AUTODOP/'autodop', '*.py'), (templates, '*'),
                (ROOT/'fe/fe-core/src/main/java/com/starrocks', '**/*.java'),
                (ROOT/'be/src/util', 'cpu_usage_info.*')]
            for p in sorted(directory.glob(pattern)) if p.is_file()}}
    (out / 'BUILD_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    run('docker', 'save', '-o', out / 'images.tar', *images)
    shutil.rmtree(context)
    print(f'Built offline images in {out}; run the documented smoke test before archiving.')


if __name__ == '__main__':
    main()
