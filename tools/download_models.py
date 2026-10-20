"""User-run download of PUBLIC model weights only. Never reads patient files."""
import argparse
from pathlib import Path
import tarfile
import urllib.request

PREFIX = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/'


def safe_extract(archive, target):
    target = Path(target).resolve()
    target.mkdir(parents=True,exist_ok=True)
    with tarfile.open(archive) as tar:
        files = [m for m in tar.getmembers() if m.isfile()]
        if not files or any(m.issym() or m.islnk() or m.isdev() for m in tar.getmembers()):
            raise ValueError('模型包包含不允许的成员')
        paths = [Path(m.name.replace('\\','/')) for m in files]
        # Model archives normally have one model-name directory.
        strip_root = all(len(p.parts)>1 and p.parts[0]==paths[0].parts[0] for p in paths)
        for member,relative in zip(files,paths):
            if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
                raise ValueError('模型包路径越界')
            if strip_root:
                relative = Path(*relative.parts[1:])
            output = (target/relative).resolve()
            if not output.is_relative_to(target):
                raise ValueError('模型包路径越界')
            output.parent.mkdir(parents=True,exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise ValueError('无法读取模型成员')
            with source,output.open('wb') as stream:
                for block in iter(lambda:source.read(1024*1024),b''):
                    stream.write(block)


def main():
    parser = argparse.ArgumentParser(description='只下载公开的本地 OCR 权重；无需病例')
    parser.add_argument('--version',choices=['v6','v5'],default='v6')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]/'models'
    names = ['PP-OCRv6_medium_det','PP-OCRv6_medium_rec'] if args.version=='v6' else ['PP-OCRv5_server_det','PP-OCRv5_server_rec']
    names.append('PP-LCNet_x1_0_doc_ori')
    root.mkdir(parents=True,exist_ok=True)
    for name in names:
        target = root/name
        if (target/'inference.yml').is_file():
            print(name+': already present; skipped')
            continue
        archive = root/(name+'_infer.tar')
        print('Downloading public model: '+name,flush=True)
        urllib.request.urlretrieve(PREFIX+name+'_infer.tar',archive)
        safe_extract(archive,target)
        if not (target/'inference.yml').is_file():
            raise ValueError('模型包结构不符合预期，需手动检查')
        archive.unlink()
        print(name+': ready')


if __name__=='__main__':
    main()
