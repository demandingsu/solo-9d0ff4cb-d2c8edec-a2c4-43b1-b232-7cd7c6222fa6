# 招标截标状态核对

一个面向采购专员的离线可演示网页应用。输入**项目编号**和**公告列表网址**后，系统会：

1. 从列表页出发，仅跟踪同一站点的分页链接；
2. 抓取与项目编号匹配的公告详情页；
3. 按发布时间串联原公告、更正公告和取消公告；
4. 合并跨页或镜像重复公告；
5. 输出“可投标 / 已截止 / 已取消 / 待核实”，并列明结论依据的标题、发布时间和来源链接。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

启动后访问：

- 核对应用：<http://localhost:5000>
- 本地分页样本站点：<http://localhost:8080>
- 样本列表页：<http://localhost:8080/notices/page-1.html>

Compose 包含两个服务：

- `web`：Flask + requests + BeautifulSoup 核对应用；
- `sample-site`：使用 Python 静态文件服务器提供的本地分页样本站点。

Web 容器会把浏览器可访问的 `http://localhost:8080` 自动映射为容器网络内的 `http://sample-site:8080`，因此页面中展示的仍然是宿主机可点击的来源链接。

## 样本项目

以样本站点当前虚构数据、系统日期为 2026-11-11 时为例：

| 项目编号 | 演示场景 | 预期结论 |
|---|---|---|
| `GSB-2026-001` | 原公告后两次更正，跨页重复公告被合并 | 可投标，截标时间 2026-11-27 17:00 |
| `GSB-2026-002` | 取消公告后还有普通延期更正 | 已取消，后续更正不恢复招标 |
| `GSB-2026-003` | 同一发布时间两条更正给出不同截标时间 | 待核实 |
| `GSB-2026-004` | 相关更正详情页返回 404 | 待核实 |
| `GSB-2026-005` | 截标时间已过 | 已截止 |

> “可投标/已截止”会随运行时的系统日期变化；样本项目的取消、冲突和详情失败规则不变。

## 样本站点页面结构

样本站点模拟常见政府采购公告结构：

- 列表页：`sample_site/notices/page-1.html`、`page-2.html`、`page-3.html`
  - 每条公告位于 `.notice-item`；
  - 详情链接位于条目的 `h2 a`；
  - 项目编号位于 `.project-no` 或 `data-project-no`；
  - 发布时间使用 `<time datetime="...">`；
  - 分页链接位于 `.pagination`，包含页码和 `rel="next"`。
- 详情页：`sample_site/detail/*.html`
  - 标题位于 `h1`；
  - 项目编号、公告类型和发布日期位于 `.publish-meta`；
  - 正文包含“投标截止时间”“延期至”等常见表述。

抓取器并不只依赖这些固定 class：它还会识别常见的 `article/li/tr` 列表结构、分页文字、详情页 `meta` 时间、中文日期格式，以及“响应文件提交截止时间”“开标时间”等标签。

## 判定规则

- **可投标**：未发现取消公告，且按时间链得到的最新截标时间晚于当前时间。
- **已截止**：未发现取消公告，但最新截标时间不晚于当前时间。
- **已取消**：发现取消/终止/废标类公告；其后的普通更正公告只展示在时间链中，不恢复招标。
- **待核实**：
  - 与项目相关的详情页抓取失败；
  - 同一发布时间的更正公告给出互相冲突的截标时间；
  - 分页或详情数量超过安全上限；
  - 缺少关键发布时间或有效截标时间；
  - 未找到与项目编号匹配的公告。

## 本地开发（不使用 Docker）

需要 Python 3.11+。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 终端 1：启动样本站点
python -m http.server 8080 --directory sample_site

# 终端 2：启动核对应用
python app.py
```

然后访问 <http://localhost:5000>。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `5000` | Web 应用监听端口 |
| `DEMO_PROJECT_NO` | `GSB-2026-001` | 表单默认项目编号 |
| `DEMO_LIST_URL` | `http://localhost:8080/notices/page-1.html` | 表单默认列表网址 |
| `SAMPLE_SITE_PUBLIC_URL` | 空 | 浏览器侧样本站点地址 |
| `SAMPLE_SITE_INTERNAL_URL` | 空 | 容器网络内样本站点地址 |
| `MAX_LIST_PAGES` | `15` | 最多抓取的列表分页数 |
| `MAX_DETAIL_PAGES` | `80` | 最多抓取的详情页数 |
| `FLASK_SECRET_KEY` | 开发默认值 | Flask 密钥，生产环境应覆盖 |

## 测试

核心日期解析和状态判定测试只依赖 Python 标准库：

```bash
python -m unittest discover -s tests
```

## 边界说明

- 仅抓取静态 HTML，不执行 JavaScript；
- 分页跟踪限制在同一站点（同一 scheme、主机和端口）；
- 单个 HTML 页面默认最大 5 MB；
- 详情页请求超时后会将相关项目标记为“待核实”，不会猜测结论；
- 抓取结果用于辅助核对，正式投标前仍应以采购人或代理机构的书面确认为准。
