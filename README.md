# 🎵 Music Downloader

基于 [GD Studio Music API](https://music-api.gdstudio.xyz/api.php) 的音乐搜索/试听/下载工具。

## ⚠️ 免责声明

- 本项目基于 GD Studio 提供的公开 API，仅供**学习参考**，**严禁用于商业用途**。
- 所有音乐资源来自网络，版权归原作者所有。请支持正版。
- 使用本工具即视为同意：仅限个人学习使用，不得下载、传播或商用。
- 若使用本项目涉及的 API，请注明出处 **"GD 音乐台 (music.gdstudio.xyz)"**。
- 如有侵权，请联系 GD Studio 删除。

> Written by GD Studio. License: CC BY-NC 4.0  
> API 文档: https://music-api.gdstudio.xyz/api.php  
> 联系 GD Studio: B站私信 GD-Studio

## 功能

- 🔍 多音源搜索（网易云 / 酷我 / JOOX）
- ▶️ 流媒体试听
- ⬇️ 在线下载（320kbps / 无损）
- 🖼️ 封面自动嵌入 MP3 ID3 标签（多音源 fallback）
- 🌐 Web UI — Flask + Tailwind CSS
- 📄 CLI 命令行

## 安装

```bash
git clone https://github.com/<user>/music_downloader.git
cd music_downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 使用

### Web UI

```bash
./webui.py                    # http://127.0.0.1:8080
```

### CLI

```bash
./music_dl.py search 周杰伦
./music_dl.py download <ID> --name 大鱼 --artist 周深 --pic
```

## 许可

本项目采用 [CC BY-NC 4.0](LICENSE) 许可，严禁商用。
