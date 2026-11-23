# 岚桥绝缘复检台账

晨班工艺员从试验记录取得各分支的概率与耗时，登记 A 初检站和 B 确认站的流程。站员按修订号发行两份证据。夜班接线员按实收件固定当轮接线，班长在站库下线后核定截止前的完结比例和永不完结比例，用于决定是否加开复检工位。次班更正试验依据后，仍可领取原排班报告及回执。

## 运行

使用 Python 3.11 和标准库，在项目根目录运行。

```sh
python3 -B check_project.py
python3 -B check_scan_fixture.py
python3 -B -m retest.demo
```

`check_project.py` 由控制台物化，执行 `tests` 包中的完整 unittest 回归（含 V1 原回归与 V2 扫描条件核验），并离线核对 `examples/scan.json` 的 V1 投影与 V2 条件结果及存档。`check_scan_fixture.py` 可单独运行同一冻结样例的双版核报。演示读取 `examples/` 的日常与勘误数据并在临时目录建库，输出实测报告；日常样例在第 1、3 拍累计完成 1/2、3/4，勘误后分别为 0、3/4，两版均最终必完结。扫描演示在站库删除后由中心离线签报，并按号领回原观测、条件结果、双件出处与回执。

## Python 接口

存储类接受空闲的 `sqlite3.Connection`。站端用 `RevisionStore` 和 `EvidenceRegistry`，中心用 `ManifestStore` 和 `ReportLedger`。初次使用及重开均可调用各自的 `initialize()`。连接由调用者关闭。写入接口持有自己的事务，已有事务须由调用者先结束。

```python
from retest.revisions.model import RevisionDraft, Transition, TrialSource
from retest.revisions.registry import register
from retest.revisions.storage import RevisionStore
from retest.evidence.issue import issue
from retest.evidence.registry import EvidenceRegistry
from retest.wiring.binding import bind
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore
from retest.analysis.review import ReviewRequest, ScanReviewRequest, review
from retest.reports.ledger import ReportLedger
from retest.reports.retrieve import get
```

- `register(revisions, draft) -> Revision` 固定 `RevisionDraft(station, revision_id, source, entries, exits, transitions, predecessor=None)`。`TrialSource(work_order, round_id, trial_id)` 保存上游出处。入口映射到私有状态名。`Transition(target, probability, ticks, is_exit=False, scan=None)` 的目标为私有状态，`is_exit=True` 时为公开出口，`scan` 为该分支耗时结束时的公开扫描名，`None` 表示无扫描。概率可用 `Fraction`、整数或 `Fraction` 可解析的字符串。每个状态的分支和为 1。拍数为非负整数。勘误用新号和本站已有前版号登记。登记、重开读回及显式选版均保留扫描标记，旧修订读回时未记录的标记视作 `None`。
- `issue(revisions, issued, station, revision_id, *, version=1) -> str` 按原号取得修订并返回 JSON 正文，`version` 只能取非布尔整数 `1` 或 `2`。V1 沿用首离站口径并忽略扫描；`version=2` 发行带扫描证据的固定原件，把私有状态替换为不透明节点，保全公开入口、出口与扫描后的走向，即使该修订没有扫描。`EvidenceRegistry.read(piece_id)` 领取原件，件号是规范 JSON 内容的 SHA-256 摘要。`decode_evidence(body)` 离线读取 V1 的 `StationEvidence` 或 V2 的 `StationScanEvidence`，并核对件号、证据结构、内部引用与质量守恒；坏正文或校验失败以 `ValueError` 拒收。
- `bind(manifests, manifest_id, source, bodies, connections) -> Manifest` 固定两站同工单同轮的原始正文及接线出处。两件必须同版，V1 与 V2 混收抛 `ValueError`。`connections` 是 `(PortRef(件号, 出口名), 目的地)` 对序列。目的地为 `PortRef(件号, 入口名)` 或 `None`，后者表示完结。各出口恰好出现一次。入口与出口按方向解析，同名端口可通过件号区分。
- `review(manifests, ledger, ReviewRequest(manifest_id, start, deadline)) -> IssuedReport` 核定双 V1 站件。`ScanReviewRequest(manifest_id, start, observed_through, scans, deadline)` 只接受双 V2 站件，`observed_through` 为 C、`deadline` 为 D，要求非布尔整数且 `0 ≤ C ≤ D`；`scans` 为 `(tick, name)` 序列，拍数在 `[0,C]` 内且按拍非降序，同拍保留实录顺序，允许空记录、重复扫描和零拍回流。每条分支在耗时结束时先计扫描再按接线同拍接续或完结。相容路径在窗内的全部扫描与观测逐项一致，已完结、在途或永久静默质量均计入支持。`snapshot` 增 `observed_through`、`scans`（独立二元组元组）和 `support`；条件按期与条件永不完结用同一支持作分母，均为 `Fraction`，支持为零时仍签报告和同号回执且两项条件值为 `None`。报告快照携带原请求、双件出处及接线。`receipt.report_id` 是本中心账簿的签发号。
- `get(ledger, report_id) -> IssuedReport` 按号领取原快照及回执，关闭站库并重开中心后仍可领回原观测、条件结果、原接线、双件出处和回执。读取无需站库和分析求解器。未知修订、站件、接线和报告以 `KeyError` 表示。非法数据、错端口、错版或身份冲突以 `ValueError` 表示。SQLite 写入错误会传给调用者，定稿与回执在同一事务内，任一写入失败都撤回本次签存。

站端求解在首次到达公开出口时结束计时。中心按剩余拍数累计完结概率，用精确方程结清同拍接续，并另行核算最终可完结质量。回归覆盖等边缘换时、长尾首出、串行接线、重入、同拍可退出循环、永久互返、站内滞留、错件拒绝、回执拒写及原号交班。演示和测试使用固定输入及临时工作目录。
