"""Compatibility for Paddle's Windows native file reader."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def native_model_dir(folder):
    folder = Path(folder).resolve()
    if os.name != 'nt' or str(folder).isascii():
        return str(folder)
    # Only public model files are staged. Patient files stay in the workspace.
    files = sorted(p for p in folder.glob('inference.*') if p.is_file())
    if not (folder/'inference.yml').is_file() or not (folder/'inference.pdiparams').is_file() or not any(
        (folder/name).is_file() for name in ('inference.json', 'inference.pdmodel')
    ):
        raise RuntimeError('本地模型文件不完整；需要 inference.yml、模型结构和权重文件')
    signature = repr([(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files])
    key = hashlib.sha256((str(folder)+signature).encode('utf-8')).hexdigest()[:24]
    root = Path(tempfile.gettempdir())/'medocr_model_cache'
    if not str(root).isascii():
        raise RuntimeError('Windows 模型缓存路径包含中文；请将 TEMP 和 TMP 设置为可写的英文路径后重启程序')
    target = root/key
    for source in files:
        output = target/source.name
        if output.is_file() and output.stat().st_size == source.stat().st_size:
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent sessions never expose a partially copied weight file.
        fd, temporary = tempfile.mkstemp(dir=target, prefix='.copy_')
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return str(target)
