#!/usr/bin/env python3
"""Build offline Docker images from compiled FE, a Java runtime image and a policy."""
import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AUTODOP = ROOT / 'tools/autodop'
sys.path.insert(0, str(AUTODOP))
from autodop.training import file_sha256
from autodop.model import load_policy_model


def run(*command):
    subprocess.run(list(map(str, command)), check=True)


def audit_image_layers(archive, expected_tags):
    """Reject BE content even if a later image layer hides or deletes it."""
    with tarfile.open(archive) as tar:
        manifests = json.load(tar.extractfile('manifest.json'))
        tags = [tag for entry in manifests for tag in entry.get('RepoTags', [])]
        if set(tags) != set(expected_tags):
            raise ValueError(f'Unexpected images in release: {tags}')
        layers = sorted({layer for entry in manifests for layer in entry['Layers']})
        count = 0
        for name in layers:
            with tarfile.open(fileobj=tar.extractfile(name), mode='r|*') as layer:
                for entry in layer:
                    path = entry.name.lstrip('./')
                    count += 1
                    if (path in ('opt/starrocks/be', 'opt/starrocks/cn', 'starrocks_be')
                            or path.startswith(('opt/starrocks/be/', 'opt/starrocks/cn/'))
                            or path.endswith('/starrocks_be')):
                        raise ValueError(f'BE content found in saved image layer: {path}')
    return {'status': 'passed', 'image_tags': tags, 'layers_scanned': len(layers),
            'entries_scanned': count, 'be_paths_found': 0,
            'scope': 'Every saved layer, including ancestor layers; not just the merged filesystem.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory only')
    parser.add_argument('--policy-model', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--fe-base-image', default='eclipse-temurin:17-jre-jammy',
                        help='Java 17 runtime image; must not contain a BE')
    args = parser.parse_args()
    policy = args.policy_model.resolve()
    load_policy_model(policy)  # Fail before creating/building a release.
    base = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.fe_base_image]))[0]
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
    for name in ['Dockerfile.autodop', 'Dockerfile.fe', 'fe.conf', 'smoke.py']:
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
        '-t', 'autodop/service:20260926-fe-only', context)
    (context / '.dockerignore').write_text('autodop/\nmodels/\n')
    # BuildKit interprets a bare image ID as a registry name in FROM. A unique
    # local tag pins the already-audited base without a registry lookup.
    base_tag = 'autodop/fe-release-base:' + base['Id'].split(':')[1][:16]
    run('docker', 'tag', base['Id'], base_tag)
    run('docker', 'build', '--build-arg', 'FE_BASE_IMAGE=' + base_tag, '-f', context / 'Dockerfile.fe',
        '-t', 'autodop/fe:20260926', context)
    images = ['autodop/service:20260926-fe-only', 'autodop/fe:20260926']
    shutil.copy2(templates / 'compose.yaml', out)
    shutil.copy2(templates / 'smoke.py', out)
    shutil.copy2(templates / 'check_release.py', out)
    shutil.copytree(context / 'models', out / 'models')
    shutil.copytree(AUTODOP / 'docs', out / 'docs')
    (out / 'README.md').write_text(
        '# AutoDOP 离线运行包\n\n'
        '- [使用文档](docs/USAGE.md)\n'
        '- [验证记录](docs/VALIDATION.md)\n'
        '- [实验报告索引](docs/EXPERIMENTS.md)\n\n'
        '先导入 images.tar，再按使用文档启动 Compose 和运行 check。\n'
        '项目源码和历史脚本请从 GitHub 获取：\n'
        'https://github.com/Areedd/autodop/tree/codex/autodop-release-20260925\n', encoding='utf-8')
    shutil.copy2(ROOT / 'LICENSE.txt', out)
    shutil.copy2(ROOT / 'NOTICE.txt', out)
    shutil.copytree(ROOT / 'licenses-binary', out / 'licenses-binary')
    packages = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
        'autodop/service:20260926-fe-only', 'python', '-m', 'pip', 'freeze'], text=True)
    (out / 'PYTHON_PACKAGES.txt').write_text(packages)
    manifest = {'status': 'built_not_yet_tested', 'platform': 'linux/amd64',
        'git_head_at_build': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'policy_sha256': file_sha256(policy), 'fe_jar_sha256': file_sha256(jar),
        'be_included': False, 'project_source_archive_included': False, 'fe_base_image_id': base['Id'],
        'images': {tag: json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]['Id'] for tag in images},
        'source_sha256': {str(p.relative_to(ROOT)): file_sha256(p)
            for directory, pattern in [(AUTODOP/'autodop', '*.py'), (templates, '*'),
                (ROOT/'fe/fe-core/src/main/java/com/starrocks', '**/*.java')]
            for p in sorted(directory.glob(pattern)) if p.is_file()}}
    (out / 'BUILD_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    run('docker', 'save', '-o', out / 'images.tar', *images)
    validation = out / 'validation'
    validation.mkdir()
    audit = audit_image_layers(out / 'images.tar', images)
    (validation / 'NO_BE_LAYERS.json').write_text(json.dumps(audit, indent=2) + '\n')
    shutil.rmtree(context)
    print(f'Built offline images in {out}; run the documented smoke test before archiving.')


if __name__ == '__main__':
    main()
