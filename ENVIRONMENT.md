# 手动配置环境

你已有的 Conda medocr 环境和三个 OCR 权重已通过本机 GPU 合成扫描页验证，无需重新建立环境。本机 Ollama 模型也已下载并通过无病例基础推理测试；病例语义提取效果仍需验收，接入设置见第 5 节。

## 当前 Ollama 模型位置

已将模型库迁移到 `F:\医疗质量管理\OCR\models`，并把用户级 `OLLAMA_MODELS` 设置为该目录。Ollama 权重位于 `blobs`，模型清单位于 `manifests`，模型元数据位于 `metadata`。这些目录与三个 OCR 模型目录共同保存在 models 下。

`qwen3:4b-instruct-2507-q4_K_M` 的实际权重文件为：

```text
F:\医疗质量管理\OCR\models\blobs\sha256-85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9
```

迁移经过逐文件 SHA-256 核对和本机推理验证，C 盘原模型库副本已清除。迁移记录在 `work/model_migration/`。Ollama 软件本身仍安装在 C 盘。

## 已有 Conda 环境：直接启动

```powershell
conda activate medocr
Set-Location -LiteralPath 'F:\医疗质量管理\OCR'
python -m medocr doctor
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false --server.maxUploadSize 512
```

本机已验证的版本：Python 3.12.15、paddlepaddle-gpu 3.2.2、paddleocr 3.7.0、Streamlit 1.65.0。GPU 日志仍提示编译 cuDNN 9.9 与本机 9.5 版本差异；当前两份合成扫描 PDF 推理成功，暂未调整你的环境。真实病例长批次稳定性还需验收。

如在另一台机器重建，Conda 指令为：

```powershell
conda create -n medocr python=3.12 -y
conda activate medocr
Set-Location -LiteralPath 'F:\医疗质量管理\OCR'
python -m pip install -e .
python -m pip install paddlepaddle-gpu==3.2.2 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
python -m pip install -e '.[ocr]'
python tools\download_models.py --version v6
python -m medocr doctor
```

以下 venv 步骤为不用 Conda 时的备选，不需要在已有 medocr 环境中再执行。

## 1. 创建项目环境

```powershell
Set-Location -LiteralPath 'F:\医疗质量管理\OCR'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

若没有 `py`，将第二行替换为你选择的 Python 3.12 可执行文件的绝对路径：

```powershell
& '你的Python安装目录\python.exe' -m venv .venv
```

基础环境安装后可以启动界面、导入 PDF、阅读文字层、人工复核和计算；扫描页 OCR 还需要下面的 PaddlePaddle、PaddleOCR 与权重。

## 2. 安装 OCR 推理环境

当前检查到 RTX 4060 Laptop、8 GB 显存、NVIDIA 驱动 572.83。先使用 CUDA 12.6 对应的 Windows PaddlePaddle GPU 包，不使用要求更新驱动的 CUDA 12.9 包。

```powershell
.\.venv\Scripts\python.exe -m pip install paddlepaddle-gpu==3.2.2 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
.\.venv\Scripts\python.exe -m pip install -e '.[ocr]'
.\.venv\Scripts\python.exe -c "import paddle; paddle.utils.run_check(); print('GPU:', paddle.device.is_compiled_with_cuda())"
```

GPU 包安装失败时，可以先跑 CPU 版本。不要在同一环境保留 CPU、GPU 两种 PaddlePaddle 包：

```powershell
.\.venv\Scripts\python.exe -m pip uninstall paddlepaddle paddlepaddle-gpu
.\.venv\Scripts\python.exe -m pip install 'paddlepaddle>=3.2,<4'
```

然后复制 `config.json` 为 `config.local.json`，将 `ocr.device` 改为 `cpu`。图形界面优先读取 `config.local.json`；命令行需加 `--config config.local.json`。

软件版本范围写在 pyproject.toml；当前 OCR 配置使用文档中的 PP-OCRv6，需要 PaddleOCR 3.7 或以上。安装成功后保存本机真实依赖版本：

```powershell
.\.venv\Scripts\python.exe -m pip freeze | Set-Content -Encoding utf8 requirements.lock
```

这份锁文件应在本机跑通后生成，不把未安装、未验证的依赖伪称为已测试版本。

## 3. 下载本地 OCR 权重

可以手动从 [官方模型表](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/OCR.html) 下载三个 **Inference Model**，或运行项目脚本：

```powershell
.\.venv\Scripts\python.exe tools\download_models.py --version v6
```

脚本只下载公开模型，不读取任何病例。官方下载链接与落盘位置如下：

| 模型 | 下载文件 | 解压后的本地目录 |
|---|---|---|
| PP-OCRv6_medium_det | [检测模型](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv6_medium_det_infer.tar) | models/PP-OCRv6_medium_det |
| PP-OCRv6_medium_rec | [识别模型](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv6_medium_rec_infer.tar) | models/PP-OCRv6_medium_rec |
| PP-LCNet_x1_0_doc_ori | [页面方向模型](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-LCNet_x1_0_doc_ori_infer.tar) | models/PP-LCNet_x1_0_doc_ori |

模型目录内应直接有 `inference.yml` 和对应权重，而不是多套一层目录。病例运行时必须已有这些模型目录，程序不会为了处理病例隐式下载模型。

Windows 下，若模型所在路径包含中文，程序会将公开权重复制到 `%TEMP%/medocr_model_cache` 的英文缓存目录后加载；原模型仍保留在项目中。该缓存只存模型文件。病例图片通过 Python 在内存中解码，避免底层文件读取器的中文路径问题。临时目录本身也应为英文路径。

如果 PP-OCRv6 在安装版本或本机设备上不兼容，可先使用 PP-OCRv5 对照方案：

```powershell
.\.venv\Scripts\python.exe tools\download_models.py --version v5
```

在 `config.local.json` 中改为：

```json
{
  "version": "PP-OCRv5",
  "detection_name": "PP-OCRv5_server_det",
  "recognition_name": "PP-OCRv5_server_rec",
  "detection_dir": "models/PP-OCRv5_server_det",
  "recognition_dir": "models/PP-OCRv5_server_rec"
}
```

上述是替换 `ocr` 对象中这五个字段，其他配置须保留；不要用这段片段覆盖整份配置文件。

## 4. 检查与启动

```powershell
.\.venv\Scripts\python.exe -m medocr doctor
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\start.ps1
```

使用本地配置时，doctor 命令改为：

```powershell
.\.venv\Scripts\python.exe -m medocr --config config.local.json doctor
```

如果 PowerShell 不允许运行 start.ps1，直接使用下列命令，不必修改系统执行策略：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false --server.maxUploadSize 512
```

浏览器访问 `http://127.0.0.1:8501`。上传上限设为 512 MB，示例病案约 100 MB，可导入。也可以直接填写 PDF 的本地路径，避免创建上传副本。

## 5. 可选本机语义辅助

默认规则模式无需 Ollama，也无需安装语义模型。网页勾选“使用本机语义模型辅助疑难片段（可选）”，或命令行添加 --llm 时才调用；下述步骤只用于启用辅助。模型不可用不阻断规则处理。

从 [Ollama Windows 官方页面](https://ollama.com/download/windows) 下载并手动安装。程序不会替你安装。当前机器已部署 qwen3:4b-instruct-2507-q4_K_M 并完成基础推理与病例抽样测试；全文临床提取仍有证据与语义问题，尚未通过医院准确率验收。实际显存还包含上下文缓存，不保证换机后满足运行条件。

安装后，先退出托盘中的 Ollama。在用户环境变量中配置 OLLAMA_NO_CLOUD=1，再启动 Ollama，确保云功能已关闭；[官方配置说明](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features)。PowerShell 手动配置指令为：

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_NO_CLOUD','1','User')
$env:OLLAMA_NO_CLOUD = '1'
ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama list
```

下载完成后可断开外网进行病例处理。Ollama Windows 应用通常已启动本机服务；如果未启动，在另一个终端运行 ollama serve，已有服务时不要重复启动。保持服务监听 127.0.0.1:11434。

在程序“本机语义模型与医院参考资料”中选择 Ollama、启用模型，地址填 http://127.0.0.1:11434，名称填 qwen3:4b-instruct-2507-q4_K_M，保存后刷新并测试连接。也可以复制 config.json 为 config.local.json，只替换 llm 对象：

```json
{
  "enabled": true,
  "protocol": "ollama",
  "base_url": "http://127.0.0.1:11434",
  "model": "qwen3:4b-instruct-2507-q4_K_M",
  "timeout_seconds": 180,
  "max_chars_per_page": 4000,
  "max_output_tokens": 4096,
  "context_tokens": 16384,
  "fact_types_per_request": 4
}
```

保留配置中的其他字段。图形界面优先读取 config.local.json；命令行使用 --config config.local.json。程序只允许回环地址、禁用代理与重定向、拒绝 cloud 模型名称。Ollama 自身也需关闭云功能，不能用本机地址转发云端服务。

原生 Ollama 请求使用 JSON 输出、关闭思考、禁止静默截断；超长输出和无依据证据会记为失败，重跑可继续。抽取结果仍需复核，布尔语义、事件对应关系和规范完整性尤其需要医生确认。

程序在每份病例的语义提取后请求卸载 Ollama 模型，并在语义调用前释放 OCR 缓存分配。8 GB 显存仍可能不足；若出现显存错误，先把 ocr.device 改为 cpu，或单独完成 OCR 后再调用语义提取，不要直接扩大模型。语义块越小请求越多；建议先用 4000 字块完成小批次验收。

## 6. 可选的表格 / 文档视觉模型

主力 OCR 可以先跑完整流程，暂不需要另外两套模型。代码提供 `structure`、`vl` 适配入口；它们需要你按官方教程准备 PP-StructureV3 或 PaddleOCR-VL 的完整 PaddleX YAML，配置全部涉及模型的本地绝对路径，再设置顶层 `structure_config` 或 `vl_config`。

为避免隐式下载，代码检查 YAML 中每个 `model_name` 都有有效的本地 `model_dir`。这些可选后端尚未在你的机器上验证，不属于当前自动候选提取的运行前置条件。原图检查仍使用原 PDF；OCR 文本框是模型坐标，当前界面没有实现原图坐标叠加。

- [PP-StructureV3 本地集成](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
- [PaddleOCR-VL 本地部署](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html)
- [Windows PaddlePaddle 安装](https://www.paddlepaddle.org.cn/documentation/docs/en/install/pip/windows-pip_en.html)

真实病例 OCR 准确率、GPU 运行速度和语义模型抽取效果，需环境配置完成后再以人工标注病例验收。
