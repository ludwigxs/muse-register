# Muse Register

用于 Muse 注册流程自动化的本地 Web 工具。项目默认不包含任何账号、银行卡、代理、邀请码或第三方服务凭证；这些内容只能放在本机 `.env` 或通过 Web 界面导入。

## 安全说明

不要提交以下内容：

- `.env`、`.card_key`、数据库文件和 `data/` 日志
- 邮箱账号/密码、OAuth client ID、refresh token、IMAP 授权码
- 银行卡号、有效期、CVV、ZIP、代理用户名和密码
- Roxy API Token、Workspace/Project ID、邀请码
- 个人邮箱文件或任何真实站点地址

仓库已通过 `.gitignore` 排除这些文件。若凭证曾经被提交过，请立即撤销/轮换，并清理 Git 历史后再公开仓库。

## 环境要求

- Windows 10/11
- Python 3.11+
- RoxyBrowser（使用 `roxy` 驱动时）
- 可用的邮箱取码来源（Outlook、IMAP 或 Generic API）

## 安装

```powershell
git clone <你的仓库地址>
cd muse-register
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

## 配置

编辑本机 `.env`，至少按实际使用方式填写：

```dotenv
# roxy 或 local
REGISTRATION_DRIVER=roxy

# Roxy 本地 API；Token、Workspace ID 只保存在本机
ROXY_API_BASE=http://127.0.0.1:50100
ROXY_API_TOKEN=
ROXY_WORKSPACE_ID=
ROXY_PROJECT_ID=

# 邮箱来源：outlook / imap / generic_api
EMAIL_SOURCE=outlook
OUTLOOK_API_BASE=

# 代理可选；每行一个，或在 Web 界面填写
PROXY_POOL=
PROXY_EXTRACT_API=

# 可选邀请码
INVITE_CODE=
```

也可以启动后在 Web 界面的“邮箱池、代理池、卡池、指纹浏览器”中配置。真实数据会写入本地数据库，不会自动上传。

## 启动

```powershell
python web.py --host 127.0.0.1 --port 8801
```

打开 http://127.0.0.1:8801/ 。Windows 下也可运行项目中的启动脚本。

使用 Roxy 时，先启动 RoxyBrowser 本地客户端，确认 API 地址和 Token 可用，再在“指纹浏览器”中选择 Roxy 并填写 Workspace ID。

## 使用流程

1. 在“邮箱池”导入邮箱或配置邮箱服务。
2. 在“卡池（可选）”添加年龄验证卡；卡号和 CVV 会加密存储在本地数据库。
3. 在“代理池”填写美国/加拿大出口代理（如流程需要）。
4. 在“注册”选择执行模式、注册数量和注册线程，点击开始注册。
5. 在“账号/任务”查看结果和实时日志；失败任务可打开日志定位问题。

## 端口与部署

默认只监听 `127.0.0.1`，适合本机使用。若需要局域网访问，请明确设置监听地址并自行配置防火墙和访问控制；不要直接将管理界面暴露到公网。

```powershell
python web.py --host 0.0.0.0 --port 8801
```

生产部署建议使用反向代理、身份认证、HTTPS，并把 `.env`、数据库和日志放在仓库目录之外或确保它们被忽略。

## GitHub 发布

首次发布前检查：

```powershell
git status --short
git ls-files
```

确认列表中没有 `.env`、`.card_key`、`muse.sqlite3`、`data/`、邮箱文件或日志后，再提交和推送：

```powershell
git add .
git commit -m "Initial public code release"
git branch -M main
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```
