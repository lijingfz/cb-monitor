"""Decide what is new since the last notification, and format email text."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from .config import Config
from .ec2query import DESCRIBE_LIMIT_PAUSE_MINUTES, NOT_OPTED_IN, REGION_UNSUPPORTED, SUPPORTED, TYPE_UNSUPPORTED, UNKNOWN_REGION
from .scanner import AVAILABLE, NONE, Cell, WeekResult
from .timeutil import is_utc, iso, parse_iso, resolve_tz, tz_label

WEEKDAYS = "一二三四五六日"
SUBJECT_MAX = 99

# ---------------------------------------------------------------- dedup ----


def select_new(
    cells: Iterable[Cell], notified: dict[str, dict], now: datetime, reappear_after_minutes: int
) -> tuple[list[Cell], dict[str, dict]]:
    """Return the cells worth notifying and the updated ``notified`` map.

    Only cells with a complete, recent measurement count. A cell is new when it was never
    notified, when its max instance count went above the highest count already
    notified, or when it was absent (queried, nothing found) for longer than
    ``reappear_after_minutes`` and came back. Decreases and disappearances are
    never notified.
    """
    grace = timedelta(minutes=reappear_after_minutes)
    updated = dict(notified)
    new: list[Cell] = []
    for cell in cells:
        record = updated.get(cell.key)
        # Fresh, or reused from a staircase measured within the refresh interval.
        # Partial results and carry-overs after a failed refresh don't count.
        measured = cell.status == AVAILABLE and not cell.partial and not cell.as_of
        if measured:
            is_new = (
                record is None
                or cell.max_count > record["count"]
                or now - parse_iso(record["last_seen"]) > grace
            )
            if is_new:
                new.append(cell)
            count = cell.max_count if is_new else record["count"]
            updated[cell.key] = {"count": count, "last_seen": iso(now), "slice_end": cell.slice_end}
        elif record is not None and cell.status != NONE:
            # Unknown this run (stale, partial, error, timeout): don't treat it as gone.
            updated[cell.key] = {**record, "last_seen": iso(now)}
    return new, updated


def prune(records: dict[str, dict], now: datetime) -> dict[str, dict]:
    """Drop records whose start slice is entirely in the past."""
    return {k: v for k, v in records.items() if parse_iso(v["slice_end"]) > now}


# ----------------------------------------------------------- formatting ----


class Fmt:
    def __init__(self, tz_name: str) -> None:
        self.tz_name = tz_name
        self.tz = resolve_tz(tz_name)
        self.label = tz_label(tz_name)
        self.utc_only = is_utc(tz_name)

    def dt(self, dt: datetime) -> str:
        text = f"{dt:%m-%d %H:%M} UTC"
        if not self.utc_only:
            text += f"（{dt.astimezone(self.tz):%m-%d %H:%M} {self.label}）"
        return text

    def stamp(self, dt: datetime) -> str:
        if self.utc_only:
            return f"{dt:%Y-%m-%d %H:%M} UTC"
        return f"{dt.astimezone(self.tz):%Y-%m-%d %H:%M} {self.label}（{dt:%Y-%m-%d %H:%M} UTC）"


def short_type(instance_type: str) -> str:
    return instance_type.split(".")[0]


def _day(dt: datetime) -> str:
    return f"{dt:%m-%d}({WEEKDAYS[dt.weekday()]})"


def slice_label(cell: Cell) -> str:
    start, end = parse_iso(cell.slice_start), parse_iso(cell.slice_end)
    last = end - timedelta(seconds=1)
    return f"开始日期 {_day(start)} ~ {_day(last)}（UTC）"


def days_label(first: int, last: int) -> str:
    return f"{first} 天" if first == last else f"{first}–{last} 天"


def compress_days(days: Iterable[int], order: tuple[int, ...]) -> str:
    """[8, 9, 10, 21, 28] -> "8–10, 21–28 天" using adjacency in ``order``."""
    index = {d: i for i, d in enumerate(order)}
    runs: list[list[int]] = []
    for d in sorted(set(days)):
        if runs and d in index and runs[-1][-1] in index and index[d] == index[runs[-1][-1]] + 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    if not runs:
        return ""
    return ", ".join(str(r[0]) if len(r) == 1 else f"{r[0]}–{r[-1]}" for r in runs) + " 天"


def _money(currency: str, amount: float) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{currency} {amount:,.2f}"


def _unit_price(offer: dict) -> Optional[float]:
    try:
        return float(offer["fee"]) / (int(offer["count"]) * int(offer["hours"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _group_by_duration(cells: list[Cell], order: tuple[int, ...]) -> list[list[Cell]]:
    """Merge neighbouring durations that have the same max count."""
    index = {d: i for i, d in enumerate(order)}
    groups: list[list[Cell]] = []
    for cell in sorted(cells, key=lambda c: c.duration_days):
        prev = groups[-1][-1] if groups else None
        if (
            prev is not None
            and prev.max_count == cell.max_count
            and cell.duration_days in index
            and prev.duration_days in index
            and index[cell.duration_days] == index[prev.duration_days] + 1
        ):
            groups[-1].append(cell)
        else:
            groups.append([cell])
    return groups


def format_cells(cells: list[Cell], cfg: Config, fmt: Fmt) -> list[str]:
    type_order = {t: i for i, t in enumerate(cfg.instance_types)}
    region_order = {r: i for i, r in enumerate(cfg.regions)}
    targets: dict[tuple[str, str], list[Cell]] = {}
    for cell in cells:
        targets.setdefault((cell.instance_type, cell.region), []).append(cell)

    lines: list[str] = []
    for itype, region in sorted(
        targets, key=lambda k: (type_order.get(k[0], 99), region_order.get(k[1], 99), k)
    ):
        lines.append(f"━━ {itype} @ {region} ━━")
        by_slice: dict[str, list[Cell]] = {}
        for cell in targets[(itype, region)]:
            by_slice.setdefault(cell.slice_key, []).append(cell)
        for slice_key in sorted(by_slice):
            slice_cells = by_slice[slice_key]
            lines.append(slice_label(slice_cells[0]))
            for group in _group_by_duration(slice_cells, cfg.durations_days):
                lines.extend(_group_lines(group, fmt))
        lines.append("")
    return lines


def _group_lines(group: list[Cell], fmt: Fmt) -> list[str]:
    offers = [o for c in group for o in c.offers]
    count = group[0].max_count
    label = days_label(group[0].duration_days, group[-1].duration_days)
    if not offers:
        return [f"  · {label}：最多 {count} 台"]
    earliest = min(offers, key=lambda o: o["start"])
    zones = "、".join(sorted({o["az"] for o in offers}))
    prices = [p for p in (_unit_price(o) for o in offers) if p is not None]
    currency = earliest.get("currency", "USD")
    if prices:
        low, high = min(prices), max(prices)
        price = _money(currency, low) if round(low, 2) == round(high, 2) else (
            f"{_money(currency, low)}–{_money(currency, high)}"
        )
        price = f" | {price}/台·时"
    else:
        price = ""
    partial = "（台数为下限：本轮未测完）" if any(c.partial for c in group) else ""
    as_of = [c.as_of for c in group if c.as_of]
    if as_of:
        partial += f"（沿用 {fmt.dt(parse_iso(min(as_of)))} 的结果，本轮未刷新）"
    example = next(c.offers[0] for c in group if c.offers)
    ex_start, ex_end = parse_iso(example["start"]), parse_iso(example["end"])
    try:
        fee = _money(example.get("currency", "USD"), float(example["fee"]))
    except (KeyError, TypeError, ValueError):
        fee = "未知"
    return [
        f"  · {label}：最多 {count} 台{partial} | 最早开始 {fmt.dt(parse_iso(earliest['start']))}"
        f" | {zones}{price}",
        f"    例：{example['id']} = {example['count']} 台 × {group[0].duration_days} 天，"
        f"{ex_start:%m-%d %H:%M} → {ex_end:%m-%d %H:%M} UTC，{example['az']}，预付 {fee}",
    ]


def criteria_line(cfg: Config) -> str:
    return (
        f"查询条件：{', '.join(cfg.instance_types)}；开始时间在 现在+{cfg.min_start_offset_days:g} 天"
        f" ~ 现在+{cfg.max_advance_days:g} 天；时长 {compress_days(cfg.durations_days, cfg.durations_days)}"
        f"（14 天以上按 7 天步长）；单个 Capacity Block 1–{cfg.max_instance_count} 台"
    )


PURCHASE_HELP = [
    "如何购买（请手动执行；Capacity Block 购买后不能取消）：",
    "  1. 用想要的台数和时长再查一次，确认报价仍然可用，并拿到报价 ID：",
    "     aws ec2 describe-capacity-block-offerings --region <区域> \\",
    "       --instance-type <实例类型> --instance-count <台数> \\",
    "       --capacity-duration-hours <小时数> \\",
    "       --start-date-range <最早开始, UTC> --end-date-range <最晚结束, UTC>",
    "  2. 购买：",
    "     aws ec2 purchase-capacity-block --region <区域> \\",
    "       --capacity-block-offering-id <cb-...> --instance-platform Linux/UNIX",
    "",
    "本邮件由 cb-monitor 自动发送。监控程序只查询、不购买。",
]


def _subject(prefix: str, detail: str) -> str:
    text = f"{prefix}{detail}"
    return text if len(text) <= SUBJECT_MAX else text[: SUBJECT_MAX - 1] + "…"


def build_new_capacity_message(cells: list[Cell], cfg: Config, now: datetime) -> tuple[str, str]:
    fmt = Fmt(cfg.display_timezone)
    regions_by_type: dict[str, list[str]] = {}
    for cell in cells:
        regions = regions_by_type.setdefault(short_type(cell.instance_type), [])
        if cell.region not in regions:
            regions.append(cell.region)
    detail = "; ".join(f"{t} {','.join(r)}" for t, r in regions_by_type.items())
    subject = _subject("[CB监控] 发现新容量: ", detail)
    body = [
        "发现新的 EC2 Capacity Blocks for ML 容量。",
        f"扫描时间：{fmt.stamp(now)}",
        criteria_line(cfg),
        "",
        "每一行表示：在该周内可以开始、该时长的 Capacity Block，单个 Block 最多能订多少台。",
        "相邻时长的最多台数相同时合并成一行。“例”是其中最短时长、最早开始的一条报价。",
        "",
        *format_cells(cells, cfg, fmt),
        *PURCHASE_HELP,
    ]
    return subject, "\n".join(body)


_DISCOVERY_LABELS = {
    TYPE_UNSUPPORTED: "实例类型不支持",
    REGION_UNSUPPORTED: "该区域不提供 Capacity Blocks",
    NOT_OPTED_IN: "区域未启用",
    UNKNOWN_REGION: "区域名无效",
}


def discovery_lines(discovery: Optional[dict], cfg: Config) -> list[str]:
    if not discovery:
        return ["监控范围：尚未探测"]
    regions = discovery.get("regions", {})
    lines = ["监控范围（每 24 小时自动探测一次）："]
    for itype in cfg.instance_types:
        ok = [r for r in cfg.regions if regions.get(r, {}).get("types", {}).get(itype) == SUPPORTED]
        lines.append(f"  {itype}：{'、'.join(ok) if ok else '无'}")
    skipped: dict[str, list[str]] = {}
    for region in cfg.regions:
        info = regions.get(region, {})
        status = info.get("status")
        if status in _DISCOVERY_LABELS:
            skipped.setdefault(_DISCOVERY_LABELS[status], []).append(region)
            continue
        for itype, type_status in info.get("types", {}).items():
            if type_status != SUPPORTED:
                reason = _DISCOVERY_LABELS.get(type_status, f"探测出错（{type_status}）")
                skipped.setdefault(reason, []).append(f"{region}/{short_type(itype)}")
    if skipped:
        lines.append("  未纳入：" + "；".join(f"{', '.join(v)}（{k}）" for k, v in skipped.items()))
    return lines


def limit_lines(snapshot: dict, cfg: Config, fmt: Fmt) -> list[str]:
    scan = snapshot.get("scan", {})
    limits = snapshot.get("limits", {})
    started = parse_iso(scan["started"]) if scan.get("started") else None
    window_start = parse_iso(scan["window_start"]) if scan.get("window_start") else None
    groups: dict[str, list[str]] = {}
    for key in sorted(limits):
        lim = limits[key]
        parts = []
        max_start = parse_iso(lim["max_start"]) if lim.get("max_start") else None
        if max_start and started:
            parts.append(f"开始时间最晚为扫描时 +{(max_start - started).total_seconds() / 86400:.1f} 天")
        if lim.get("max_end") and started:
            max_end = parse_iso(lim["max_end"])
            parts.append(f"结束时间最晚为扫描时 +{(max_end - started).total_seconds() / 86400:.1f} 天")
        if lim.get("invalid_durations_days"):
            parts.append("被拒绝的时长 " + compress_days(lim["invalid_durations_days"], cfg.durations_days))
        if lim.get("invalid_counts"):
            parts.append("被拒绝的台数 " + ", ".join(map(str, lim["invalid_counts"])))
        if not parts:
            continue
        if max_start and window_start and max_start < window_start:
            parts.append(f"→ 与“现在+{cfg.min_start_offset_days:g} 天以后开始”不重叠，本轮无法查询")
        region, itype = key.split("|", 1)
        groups.setdefault("；".join(parts), []).append(f"{region}/{short_type(itype)}")
    if not groups:
        return ["账号查询限制：未发现，按官方文档的标准范围查询（最多提前 8 周，时长最长 182 天）。"]
    lines = ["账号查询限制（最近一次扫描时 API 实际返回的上限，比官方文档的标准范围窄）："]
    for desc, keys in groups.items():
        lines.append(f"  {', '.join(keys)}：")
        lines.append(f"    {desc}")
    lines.append("  如需放开，请联系 AWS 客户经理或 Support，说明需要按官方文档提前 8 周、最长 182 天查询 Capacity Blocks。")
    return lines


def cells_from_weeks(weeks: list[dict], cfg: Config) -> list[Cell]:
    cells = []
    for data in weeks:
        cells.extend(c for c in WeekResult(**data).cells(cfg.durations_days) if c.status == AVAILABLE)
    return cells


def build_summary_message(
    snapshot: Optional[dict], stats: dict[str, int], cfg: Config, now: datetime
) -> tuple[str, str]:
    fmt = Fmt(cfg.display_timezone)
    lines = [
        "EC2 Capacity Blocks for ML 监控：每日汇总",
        f"生成时间：{fmt.stamp(now)}",
        criteria_line(cfg),
        "",
    ]
    available: list[Cell] = []
    if not snapshot:
        lines.append("⚠ 还没有任何扫描结果，请检查 Lambda 日志。")
    else:
        scan = snapshot.get("scan", {})
        finished = parse_iso(scan["finished"])
        state = "完整" if scan.get("complete") else "未完成（时间用完，部分组合未查询）"
        lines.append(f"最近一次扫描：{fmt.stamp(finished)}，耗时 {scan.get('duration_s', 0):.0f} 秒，{state}")
        age = now - finished
        if age > timedelta(minutes=3 * cfg.scan_interval_minutes):
            lines.append(
                f"⚠ 最近一次扫描是 {age.total_seconds() / 3600:.1f} 小时前，监控可能已停止，请检查 Lambda 日志。"
            )
        available = cells_from_weeks(snapshot.get("weeks", []), cfg)
    lines.append(
        "过去 24 小时：扫描 {runs} 次（完整 {complete} 次），API 调用 {calls} 次，"
        "API 错误 {errors} 次（其中限流 {throttled} 次），发送新容量通知 {sent} 封".format(
            runs=stats.get("runs", 0),
            complete=stats.get("runs_complete", 0),
            calls=stats.get("api_calls", 0),
            errors=stats.get("api_errors", 0),
            throttled=stats.get("throttled", 0),
            sent=stats.get("notifications", 0),
        )
    )
    if stats.get("describe_limit_exceeded"):
        lines.append(
            f"⚠ 过去 24 小时触发账号级 describe 配额（CapacityBlockDescribeLimitExceeded）"
            f" {stats['describe_limit_exceeded']} 次；触发的区域会暂停查询 {DESCRIBE_LIMIT_PAUSE_MINUTES} 分钟。"
        )
    lines.append("")
    if snapshot:
        lines.extend(discovery_lines(snapshot.get("discovery"), cfg))
        lines.append("")
        lines.extend(limit_lines(snapshot, cfg, fmt))
        paused = {r: t for r, t in (snapshot.get("paused") or {}).items() if parse_iso(t) > now}
        if paused:
            lines.append("暂停查询的区域（触发 describe 配额）：" + "；".join(
                f"{r} 至 {fmt.dt(parse_iso(t))}" for r, t in sorted(paused.items())))
        errors = snapshot.get("scan", {}).get("errors", [])
        if errors:
            lines.append("")
            lines.append(f"最近一次扫描的错误（最多显示 10 条，共 {len(errors)} 条）：")
            lines.extend(f"  {e}" for e in errors[:10])
        lines.append("")
    if available:
        lines.append(f"当前可用容量（最近一次扫描，共 {len(available)} 个“周 × 时长”组合）：")
        lines.append("")
        lines.extend(format_cells(available, cfg, fmt))
        lines.extend(PURCHASE_HELP)
    else:
        lines.append("当前没有符合条件的容量。")
    subject = _subject(
        f"[CB监控] 每日汇总 {now.astimezone(fmt.tz):%Y-%m-%d}: ",
        f"{len(available)} 个可用组合" if available else "暂无可用容量",
    )
    return subject, "\n".join(lines)


def build_test_message(cfg: Config, now: datetime) -> tuple[str, str]:
    fmt = Fmt(cfg.display_timezone)
    body = [
        "这是一封测试邮件。收到这封邮件说明 SNS 邮件订阅工作正常。",
        f"发送时间：{fmt.stamp(now)}",
        criteria_line(cfg),
    ]
    return "[CB监控] 测试通知", "\n".join(body)
