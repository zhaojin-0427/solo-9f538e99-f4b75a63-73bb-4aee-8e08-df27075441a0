# 波段查缺 API（FastAPI + SQLite）

植被指数（如 NDVI）要求**同一景**的红光（red）与近红外（nir）在同一网格像元上
都有效。本服务接收各景各波段的 0/1 有效掩膜，维护每像元的已提交覆盖，并按日期与
网格矩形返回连续缺口，供数据员补报。

## 启动

### Docker Compose（推荐）

```bash
docker compose up -d --build
```

- 服务：<http://localhost:8000>
- Swagger 交互文档：<http://localhost:8000/docs>（可直接在页面上提交掩膜与查缺）
- OpenAPI：<http://localhost:8000/openapi.json>
- SQLite 数据持久化在命名卷 `gapcheck-data`（容器内 `/data/gapcheck.db`）

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库路径可用环境变量覆盖：
# GAPCHECK_DB_PATH=/tmp/gapcheck.db uvicorn app.main:app
```

## API

### `POST /masks` — 提交 / 更正某景某波段掩膜

请求体（JSON）：

| 字段 | 说明 |
| --- | --- |
| `date` | UTC 日期 `YYYY-MM-DD` |
| `scene_id` | 景号 |
| `origin_x` / `origin_y` | 景网格左上角在全局网格中的坐标 |
| `band` | `red`（红光）或 `nir`（近红外），两波段可分别提交、分别更正 |
| `mask` | 行优先二维 0/1 数组，宽、高各不超过 256 格，行长度必须一致；1=有效 |

规则：

- **同景的网格位置与尺寸固定**：首次提交注册几何；之后同景号/同日期再提交若
  原点或宽高不一致，返回 `409`，不写入任何数据。
- 更正只回读**本景**的源掩膜（另一波段），只重算**本景范围**内的覆盖；其他景的
  贡献原样保留。
- 整个提交在一个 SQLite 事务（`BEGIN IMMEDIATE`）内完成，任何失败都回滚，
  已提交结果仍可查询。

### `GET /gaps` — 按日期与矩形查缺

查询参数：`date`、`origin_x`、`origin_y`、`width`、`height`
（矩形宽、高各不超过 256 格）。

判定：任一景的红光与近红外在某格都有效（两景号集合有交集），该格即**齐全**。
其余格子按缺口类别，在每一行内把连续同类格子合并成段返回：

| category | 含义 |
| --- | --- |
| `red_only` | 仅红光有效，缺近红外 |
| `nir_only` | 仅近红外有效，缺红光 |
| `different_scenes` | 两波段都有，但分属不同景，没有同一景的双波段 |
| `both_missing` | 红光与近红外均缺报 |

返回含每类计数、齐全格数与逐行连续缺口段（`y`、`x_start`、`x_end`、`length`）。

### `GET /scenes/{date}/{scene_id}` — 回读某景几何与已提交波段信息

## 数据模型与关键保证

SQLite 三张表（见 `app/db.py`）：

- `scenes`：每景固定几何（日期、景号、原点、宽高）。
- `band_masks`：每景每波段一份源掩膜 BLOB（行优先 0/1），更正按波段覆盖。
- `coverage`：每像元一行（`date,x,y` 主键，`WITHOUT ROWID`），`red_scenes` /
  `nir_scenes` 为提供有效样本的**景号 JSON 数组**。这是更正时无需读取其他景掩膜
  的关键：本景旧贡献从两个数组中移除，再按新掩膜加入即可。

对应需求的落点：

1. **掩膜和覆盖一起生效，失败仍可查原结果** — 单事务提交，异常即回滚。
2. **查询取同一已提交状态，无需读取单景掩膜** — `/gaps` 只查 `coverage` 表，
   单条带索引的范围扫描；WAL 模式下读不阻塞写，一条查询对应一个已提交快照。
3. **读取的覆盖记录最多和矩形内像元一样多** — 扫描条件为
   `x BETWEEN ... AND y BETWEEN ...`，主键即 `(date,x,y)`，每像元至多一行；
   无覆盖的像元按“两波段都缺”处理，不产生额外记录。
4. **更正仅重算本景范围** — 覆盖扫描与回写都限定在本景原点与宽高内，
   且只读取本景另一波段的源掩膜。
5. **尺寸限制** — 掩膜与查询矩形的宽、高均在 Pydantic / 查询校验中限制为
   1–256。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖：四类缺口分类、行内连续段合并、同景双波段齐全、跨景不同景判定、多景任一
齐全、更正只影响本景、几何冲突 409、失败回滚后原结果可查、256 上限、日期隔离等
14 个用例。

## 目录结构

```
app/
  config.py    # 环境配置与 256 上限
  db.py        # SQLite 连接、建表、事务边界
  models.py    # 请求/响应模型与缺口类别
  services.py  # 掩膜提交更正、覆盖重算、查缺核心逻辑
  main.py      # FastAPI 路由（/docs）
tests/         # pytest 端到端用例
Dockerfile
docker-compose.yml
requirements.txt
```
