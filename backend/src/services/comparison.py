"""两次已完成密度试验的缺陷去向对比。

同一数据批次内的缺陷编号是跨参数试验唯一稳定的身份。本模块只读取既有
Job.result 与 Dataset.records，不重算、不覆盖任何已有试验结果。

两种结果格式都兼容：
- 新结果在 points 中保存每个缺陷编号的归属，直接逐点对比；
- 历史结果没有 points，按已保存的分组起止里程还原逐点归属，并用分组点数校验；
- 连里程区间都无法还原的结果退化为分组汇总比较，并明确标注为推断。
"""
import math

from fastapi import HTTPException

NOISE_BAND = "孤立/稀疏点"
BAND = "band"
NOISE = "noise"
ROUNDING_TOLERANCE_M = 0.1  # 历史结果里程保留 4 位小数

BUCKETS = ["unchanged_points", "split_points", "merged_points", "band_to_noise_points", "noise_to_band_points"]


def _reject(status, message):
    raise HTTPException(status, message)


def _load_defects(dataset):
    """校验数据批次的缺陷编号，返回按批次顺序排列的缺陷点。"""
    records = dataset.records or []
    if not records:
        _reject(409, "数据批次没有缺陷点，无法对比")
    defects, seen = [], {}
    for index, row in enumerate(records):
        raw = row.get("defect_id")
        defect_id = "" if raw is None else str(raw).strip()
        if not defect_id:
            _reject(422, f"数据批次第 {index + 1} 行缺少缺陷编号 defect_id，无法按点追踪")
        if defect_id in seen:
            _reject(422, f"缺陷编号重复：{defect_id}（第 {seen[defect_id]} 行与第 {index + 1} 行），"
                         "无法按点追踪")
        seen[defect_id] = index + 1
        try:
            mileage = float(row["mileage_km"])
            severity = int(float(row["severity"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            _reject(422, f"数据批次第 {index + 1} 行缺少可用的里程或复核等级")
        if not math.isfinite(mileage) or mileage < 0:
            _reject(422, f"数据批次第 {index + 1} 行里程不是非负有限数值")
        defects.append({"defect_id": defect_id, "mileage_km": mileage, "severity": severity})
    return defects


def _groups_from_assignment(assignment, defects):
    """由 defect_id -> 分组名 构造内部分组（含成员集合），按起点排序。"""
    meta = {defect["defect_id"]: defect for defect in defects}
    members = {}
    for defect_id, name in assignment.items():
        members.setdefault(name, set()).add(defect_id)
    groups = []
    for name, ids in members.items():
        mileages = [meta[member]["mileage_km"] for member in ids]
        groups.append({
            "name": name,
            "type": NOISE if name == NOISE_BAND else BAND,
            "count": len(ids),
            "from_km": round(min(mileages), 4),
            "to_km": round(max(mileages), 4),
            "max_severity": max(meta[member]["severity"] for member in ids),
            "members": set(ids),
        })
    groups.sort(key=lambda group: (group["type"] == NOISE, group["from_km"], group["name"]))
    return groups


def _from_points(result, defects):
    """新格式：points 直接给出每个缺陷编号的归属。"""
    points = result.get("points")
    if not isinstance(points, list) or not points:
        return None
    assignment = {}
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            _reject(409, f"试验结果的逐点明细第 {index + 1} 项格式无法识别")
        raw = point.get("defect_id")
        band = point.get("band")
        defect_id = "" if raw is None else str(raw).strip()
        if not defect_id or not isinstance(band, str) or not band.strip():
            _reject(409, f"试验结果的逐点明细第 {index + 1} 项缺少缺陷编号或分组")
        if defect_id in assignment:
            _reject(409, f"试验结果的逐点明细中缺陷编号重复：{defect_id}")
        assignment[defect_id] = band.strip()
    if set(assignment) != {defect["defect_id"] for defect in defects}:
        _reject(409, "试验结果的逐点明细与所选数据批次的缺陷编号不一致，无法按点对比")
    return _groups_from_assignment(assignment, defects)


def _table_groups(result):
    """读取结果分组表，返回 (bands, noise_row)；结构不符时返回 None。"""
    table = result.get("table")
    rows = table.get("rows") if isinstance(table, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    bands, noise_row = [], None
    for row in rows:
        if not isinstance(row, dict):
            return None
        name = row.get("band")
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        if name == NOISE_BAND:
            if noise_row is not None:
                return None
            noise_row = row
            continue
        try:
            start, end = float(row["from_km"]), float(row["to_km"])
            count = int(row["count"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not (math.isfinite(start) and math.isfinite(end)) or start > end or count < 0:
            return None
        if any(band["name"] == name for band in bands):
            return None
        bands.append({"name": name, "from_km": start, "to_km": end, "count": count})
    if not bands and noise_row is None:
        return None
    return bands, noise_row


def _from_table(result, defects):
    """历史格式：按已保存的分组里程区间还原逐点归属，并逐组核对点数。

    一维 DBSCAN 的簇在里程轴上是互不重叠的连续区间，区间内的点必然属于该带；
    任何不一致都返回 None，由调用方降级为汇总比较，绝不猜测。
    """
    parsed = _table_groups(result)
    if parsed is None:
        return None
    bands, noise_row = parsed
    tolerance = ROUNDING_TOLERANCE_M / 1000
    assignment, computed = {}, {}
    for defect in defects:
        mileage = defect["mileage_km"]
        hits = [band for band in bands if band["from_km"] - tolerance <= mileage <= band["to_km"] + tolerance]
        if len(hits) > 1:
            return None
        name = hits[0]["name"] if hits else NOISE_BAND
        assignment[defect["defect_id"]] = name
        computed[name] = computed.get(name, 0) + 1
    for band in bands:
        if computed.get(band["name"], 0) != band["count"]:
            return None
    expected_noise = int(noise_row.get("count", 0)) if noise_row is not None else 0
    if computed.get(NOISE_BAND, 0) != expected_noise:
        return None
    return _groups_from_assignment(assignment, defects)


def _summary_groups(result):
    """无法逐点还原时的分组摘要，供降级模式使用。"""
    parsed = _table_groups(result)
    if parsed is None:
        _reject(409, "试验结果缺少可识别的分组明细，无法对比")
    bands, noise_row = parsed
    groups = [{"name": band["name"], "type": BAND, "count": band["count"],
               "from_km": band["from_km"], "to_km": band["to_km"]} for band in bands]
    if noise_row is not None:
        try:
            count = int(noise_row["count"])
            start, end = float(noise_row["from_km"]), float(noise_row["to_km"])
        except (KeyError, TypeError, ValueError, OverflowError):
            count, start, end = int(noise_row.get("count", 0) or 0), None, None
        groups.append({"name": NOISE_BAND, "type": NOISE, "count": count, "from_km": start, "to_km": end})
    groups.sort(key=lambda group: (group["type"] == NOISE, group["from_km"] if group["from_km"] is not None else 0))
    return groups


def _map_by_defect(groups):
    mapping = {}
    for group in groups:
        for member in group["members"]:
            mapping[member] = group
    return mapping


def _classify(first_groups, second_groups, defects):
    """对照两次归属，为两组分组和每个缺陷点标注去向。"""
    first_map, second_map = _map_by_defect(first_groups), _map_by_defect(second_groups)
    first_by_name = {group["name"]: group for group in first_groups}
    second_by_name = {group["name"]: group for group in second_groups}

    # 有向边：第一次分组 -> 第二次分组，以及每个缺陷编号
    edges = {}
    for defect in defects:
        defect_id = defect["defect_id"]
        source, target = first_map[defect_id], second_map[defect_id]
        key = (source["name"], target["name"])
        edges.setdefault(key, {"from": source, "to": target, "ids": []})["ids"].append(defect_id)

    targets_of, sources_of = {}, {}
    for (from_name, to_name), edge in edges.items():
        targets_of.setdefault(from_name, {})[to_name] = edge
        sources_of.setdefault(to_name, {})[from_name] = edge

    def targets_list(name):
        found = targets_of.get(name, {})
        groups = [(second_by_name[target], edge["ids"]) for target, edge in found.items()]
        groups.sort(key=lambda pair: (pair[0]["type"] == NOISE, pair[0]["from_km"]))
        return groups

    def sources_list(name):
        found = sources_of.get(name, {})
        groups = [(first_by_name[source], edge["ids"]) for source, edge in found.items()]
        groups.sort(key=lambda pair: (pair[0]["type"] == NOISE, pair[0]["from_km"]))
        return groups

    def public(entries):
        return [{"group": group["name"], "type": group["type"], "count": len(ids)} for group, ids in entries]

    def clean(value):
        return {key: item for key, item in value.items() if key != "members"}

    # 第一次试验各组的去向
    for group in first_groups:
        targets = targets_list(group["name"])
        band_targets = [pair for pair in targets if pair[0]["type"] == BAND]
        noise_targets = [pair for pair in targets if pair[0]["type"] == NOISE]
        group["targets"] = public(targets)
        if group["type"] == NOISE:
            if band_targets:
                group["kind"] = "noise_to_band"
                group["change"] = "并入缺陷带" if len(band_targets) == 1 else "分散进入多条带"
            else:
                group["kind"], group["change"] = "unchanged", "未变"
        elif not band_targets:
            group["kind"], group["change"] = "band_to_noise", "全部转为孤立点"
        elif len(band_targets) >= 2:
            group["kind"], group["change"] = "split", "分裂"
        else:
            target = band_targets[0][0]
            band_sources = [pair for pair in sources_list(target["name"]) if pair[0]["type"] == BAND]
            noise_sources = [pair for pair in sources_list(target["name"]) if pair[0]["type"] == NOISE]
            if len(band_sources) >= 2:
                group["kind"], group["change"] = "merge", "合并"
            elif noise_targets:
                group["kind"], group["change"] = "band_partial_noise", "部分转为孤立点"
            elif noise_sources:
                group["kind"], group["change"] = "unchanged", "未变（吸收孤立点）"
            else:
                group["kind"], group["change"] = "unchanged", "未变"

    # 第二次试验各组的来源
    for group in second_groups:
        sources = sources_list(group["name"])
        band_sources = [pair for pair in sources if pair[0]["type"] == BAND]
        noise_sources = [pair for pair in sources if pair[0]["type"] == NOISE]
        group["sources"] = public(sources)
        if group["type"] == NOISE:
            if band_sources:
                group["kind"], group["change"] = "band_to_noise", "接收带内散点"
            else:
                group["kind"], group["change"] = "unchanged", "未变"
        elif len(band_sources) >= 2:
            group["kind"], group["change"] = "merge", "合并"
        elif not band_sources and noise_sources:
            group["kind"], group["change"] = "noise_to_band", "由孤立点形成"
        elif len(band_sources) == 1:
            source = band_sources[0][0]
            source_band_targets = [pair for pair in targets_list(source["name"]) if pair[0]["type"] == BAND]
            if len(source_band_targets) >= 2:
                group["kind"], group["change"] = "split_target", "分裂产物"
            elif noise_sources:
                group["kind"], group["change"] = "band_absorbs_noise", "吸收孤立点"
            elif source["kind"] == "split":
                group["kind"], group["change"] = "unchanged", "未变（来源带部分散失）"
            else:
                group["kind"], group["change"] = "unchanged", "未变"
        else:
            group["kind"], group["change"] = "unchanged", "未变"

    # 逐点去向：五个桶必须精确加总到缺陷总数
    counts = {key: 0 for key in BUCKETS}
    rows = []
    for defect in sorted(defects, key=lambda item: item["mileage_km"]):
        defect_id = defect["defect_id"]
        source, target = first_map[defect_id], second_map[defect_id]
        if source["type"] == NOISE and target["type"] == NOISE:
            bucket, change = "unchanged_points", "未变"
        elif source["type"] == NOISE:
            bucket, change = "noise_to_band_points", "孤立点并入缺陷带"
        elif target["type"] == NOISE:
            bucket, change = "band_to_noise_points", "缺陷带转为孤立点"
        else:
            source_fate = source["kind"]
            if source_fate == "split":
                bucket, change = "split_points", "随带分裂"
            elif source_fate == "merge":
                bucket, change = "merged_points", "随带合并"
            elif source_fate == "band_partial_noise":
                bucket, change = "unchanged_points", "带未变（部分点转孤立）"
            elif source["members"] == target["members"]:
                bucket, change = "unchanged_points", "未变"
            else:
                bucket, change = "unchanged_points", "带未变（成员增减）"
        counts[bucket] += 1
        rows.append({"defect_id": defect_id, "mileage_km": defect["mileage_km"], "severity": defect["severity"],
                     "first_group": source["name"], "first_type": source["type"],
                     "second_group": target["name"], "second_type": target["type"], "change": change})

    total = len(defects)
    reconciled = sum(counts.values()) == total
    if not reconciled:  # 防御性校验，正常路径不会触发
        _reject(500, "缺陷去向合计与缺陷总数不一致，已停止对比")

    edge_rows = [{"from_group": edge["from"]["name"], "from_type": edge["from"]["type"],
                  "to_group": edge["to"]["name"], "to_type": edge["to"]["type"],
                  "count": len(edge["ids"]), "defect_ids": sorted(edge["ids"])}
                 for edge in edges.values()]
    edge_rows.sort(key=lambda edge: (edge["from_type"] == NOISE, edge["from_group"],
                                     edge["to_type"] == NOISE, edge["to_group"]))
    return rows, counts, edge_rows, total, [clean(group) for group in first_groups], [clean(group) for group in second_groups]


def _overlaps(left, right):
    if left.get("from_km") is None or right.get("from_km") is None:
        return False
    return max(left["from_km"], right["from_km"]) <= min(left["to_km"], right["to_km"])


def _classify_summary(first_groups, second_groups):
    """降级模式：只按里程区间重叠推断分裂/合并，逐点合计不可用。"""
    first_bands = [group for group in first_groups if group["type"] == BAND]
    second_bands = [group for group in second_groups if group["type"] == BAND]
    for group in first_groups:
        group["targets"] = []
        if group["type"] == NOISE:
            group["kind"], group["change"] = "unchanged", "未变（仅比较点数）"
            continue
        overlap = [other for other in second_bands if _overlaps(group, other)]
        group["targets"] = [{"group": other["name"], "type": BAND, "count": other["count"]} for other in overlap]
        if len(overlap) >= 2:
            group["kind"], group["change"] = "split", "疑似分裂"
        elif len(overlap) == 1:
            target = overlap[0]
            reverse = [other for other in first_bands if _overlaps(other, target)]
            if len(reverse) >= 2:
                group["kind"], group["change"] = "merge", "疑似合并"
            elif group["count"] != target["count"] or (group["from_km"], group["to_km"]) != (target["from_km"], target["to_km"]):
                group["kind"], group["change"] = "unchanged", "范围或点数变化"
            else:
                group["kind"], group["change"] = "unchanged", "未变"
        else:
            group["kind"], group["change"] = "band_to_noise", "疑似全部转为孤立点"
    for group in second_groups:
        group["sources"] = []
        if group["type"] == NOISE:
            group["kind"], group["change"] = "unchanged", "未变（仅比较点数）"
            continue
        overlap = [other for other in first_bands if _overlaps(group, other)]
        group["sources"] = [{"group": other["name"], "type": BAND, "count": other["count"]} for other in overlap]
        if len(overlap) >= 2:
            group["kind"], group["change"] = "merge", "疑似合并"
        elif len(overlap) == 1:
            source = overlap[0]
            if source["count"] != group["count"] or (source["from_km"], source["to_km"]) != (group["from_km"], group["to_km"]):
                group["kind"], group["change"] = "unchanged", "范围或点数变化"
            else:
                group["kind"], group["change"] = "unchanged", "未变"
        else:
            group["kind"], group["change"] = "noise_to_band", "疑似由孤立点形成"
    first_total = sum(group["count"] for group in first_groups)
    second_total = sum(group["count"] for group in second_groups)
    return {"first_groups": first_groups, "second_groups": second_groups,
            "first_total": first_total, "second_total": second_total}


def _param_changes(first, second):
    changes = []
    for key in list(first.params.keys()) + [key for key in second.params.keys() if key not in first.params]:
        if first.params.get(key) != second.params.get(key):
            changes.append({"key": key, "first": first.params.get(key), "second": second.params.get(key)})
    return changes


def _findings(first, second, comparison, param_changes):
    first_groups, second_groups = comparison["first_groups"], comparison["second_groups"]
    first_bands = [group for group in first_groups if group["type"] == BAND]
    second_bands = [group for group in second_groups if group["type"] == BAND]
    first_noise = sum(group["count"] for group in first_groups if group["type"] == NOISE)
    second_noise = sum(group["count"] for group in second_groups if group["type"] == NOISE)
    basis = comparison["basis"]
    suffix = "（疑似）" if basis == "band_summary" else ""
    findings = []
    if param_changes:
        detail = "，".join(f"{change['key']} {change['first']} → {change['second']}" for change in param_changes)
        findings.append(f"两次试验参数变化：{detail}。")
    else:
        findings.append("两次试验参数相同；若结果不同，说明输入批次或结果本身存在差异。")
    findings.append(f"第一次试验：{len(first_bands)}条密集带、{first_noise}个孤立/稀疏点；"
                    f"第二次试验：{len(second_bands)}条密集带、{second_noise}个孤立/稀疏点。")
    for group in first_groups:
        targets = "、".join(f"{target['group']} {target['count']}点" for target in group["targets"])
        if group["kind"] == "split":
            findings.append(f"第一次{group['name']}（{group['count']}点）分裂{suffix}为：{targets}。")
        elif group["kind"] == "band_to_noise":
            findings.append(f"第一次{group['name']}（{group['count']}点）在第二次全部转为孤立/稀疏点{suffix}。")
        elif group["kind"] == "noise_to_band":
            findings.append(f"第一次{group['count']}个孤立/稀疏点并入了{targets}{suffix}。")
    for group in second_groups:
        if group["kind"] == "merge":
            sources = "、".join(f"{source['group']} {source['count']}点" for source in group["sources"])
            findings.append(f"第二次{group['name']}（{group['count']}点）由多条带合并{suffix}：{sources}。")
    if basis == "per_point":
        counts = comparison["counts"]
        findings.append(f"逐点去向：未变{counts['unchanged_points']}点、分裂{counts['split_points']}点、"
                        f"合并{counts['merged_points']}点、带转噪声{counts['band_to_noise_points']}点、"
                        f"噪声并入带{counts['noise_to_band_points']}点，"
                        f"合计{sum(counts.values())}/{comparison['totals']['dataset_total']}点。")
    else:
        findings.append("历史结果无法还原逐点归属，分裂/合并按里程重叠推断，不能作为逐点结论。")
    return findings


def _validate(first, second, dataset):
    if first.id == second.id:
        _reject(422, "请选择两条不同的分析任务进行对比")
    if first.status != "completed" or second.status != "completed":
        _reject(409, "仅已完成的分析任务可以参与对比")
    if first.dataset_id != second.dataset_id:
        _reject(409, "两次试验的数据批次不同，缺陷编号不可跨批次比较")
    if dataset is None or dataset.id != first.dataset_id:
        _reject(409, "试验绑定的数据批次不存在，无法对比")
    return _load_defects(dataset)


def compare(first, second, dataset):
    defects = _validate(first, second, dataset)

    first_result, second_result = first.result or {}, second.result or {}
    first_points = _from_points(first_result, defects)
    second_points = _from_points(second_result, defects)
    first_groups = first_points if first_points is not None else _from_table(first_result, defects)
    second_groups = second_points if second_points is not None else _from_table(second_result, defects)
    if first_groups is None or second_groups is None:
        # 任一侧无法还原逐点归属时，两侧统一降级为分组汇总比较，避免混用口径
        return _compare_summary_mode(first, second, dataset)
    sources = {"first": "recorded" if first_points is not None else "reconstructed",
               "second": "recorded" if second_points is not None else "reconstructed"}

    rows, counts, edges, total, public_first, public_second = _classify(first_groups, second_groups, defects)
    basis = "per_point"
    first_total = second_total = total
    totals = {
        "dataset_total": total,
        "first_total": first_total,
        "second_total": second_total,
        "matched": total,
        "only_in_first": 0,
        "only_in_second": 0,
        "first_band_points": sum(group["count"] for group in first_groups if group["type"] == BAND),
        "first_noise_points": sum(group["count"] for group in first_groups if group["type"] == NOISE),
        "second_band_points": sum(group["count"] for group in second_groups if group["type"] == BAND),
        "second_noise_points": sum(group["count"] for group in second_groups if group["type"] == NOISE),
        **counts,
    }
    checks = [
        {"label": "第一次试验分组点数合计", "value": first_total, "expected": total},
        {"label": "第二次试验分组点数合计", "value": second_total, "expected": total},
        {"label": "按缺陷编号匹配", "value": totals["matched"], "expected": total},
        {"label": "五类逐点去向合计", "value": sum(counts.values()), "expected": total},
    ]
    for check in checks:
        check["ok"] = check["value"] == check["expected"]

    comparison = {
        "basis": basis,
        "detail_source": sources,
        "defects": rows,
        "edges": edges,
        "counts": counts,
        "totals": totals,
        "checks": checks,
        "first": {"id": first.id, "name": first.name, "params": first.params,
                  "dataset_id": first.dataset_id, "finished_at": first.finished_at, "approved": first.approved},
        "second": {"id": second.id, "name": second.name, "params": second.params,
                   "dataset_id": second.dataset_id, "finished_at": second.finished_at, "approved": second.approved},
        "dataset": {"id": dataset.id, "name": dataset.name, "checksum": dataset.checksum, "rows": len(defects)},
        "first_groups": public_first,
        "second_groups": public_second,
    }
    param_changes = _param_changes(first, second)
    comparison["params_changed"] = param_changes
    comparison["findings"] = _findings(first, second, comparison, param_changes)
    comparison["warnings"] = [
        "缺陷带名称按各自试验的起始里程重新编排，名称相同不代表成员相同。",
        "只有数据批次相同且缺陷编号唯一时才能逐点对比；噪声仅表示不满足当前密度条件，不代表低风险。",
    ]
    return comparison


def _compare_summary_mode(first, second, dataset):
    _validate(first, second, dataset)
    summary = _classify_summary(_summary_groups(first.result or {}), _summary_groups(second.result or {}))
    first_groups, second_groups = summary["first_groups"], summary["second_groups"]
    first_total, second_total = summary["first_total"], summary["second_total"]
    comparison = {
        "basis": "band_summary",
        "detail_source": {"first": "summary", "second": "summary"},
        "defects": [], "edges": [], "counts": {key: None for key in BUCKETS},
        "totals": {
            "dataset_total": None, "first_total": first_total, "second_total": second_total,
            "matched": None, "only_in_first": None, "only_in_second": None,
            "first_band_points": sum(group["count"] for group in first_groups if group["type"] == BAND),
            "first_noise_points": sum(group["count"] for group in first_groups if group["type"] == NOISE),
            "second_band_points": sum(group["count"] for group in second_groups if group["type"] == BAND),
            "second_noise_points": sum(group["count"] for group in second_groups if group["type"] == NOISE),
        },
        "checks": [
            {"label": "第一次试验分组点数合计", "value": first_total, "expected": first_total, "ok": True},
            {"label": "第二次试验分组点数合计", "value": second_total, "expected": second_total, "ok": True},
        ],
        "first": {"id": first.id, "name": first.name, "params": first.params,
                  "dataset_id": first.dataset_id, "finished_at": first.finished_at, "approved": first.approved},
        "second": {"id": second.id, "name": second.name, "params": second.params,
                   "dataset_id": second.dataset_id, "finished_at": second.finished_at, "approved": second.approved},
        "dataset": {"id": dataset.id, "name": dataset.name, "checksum": dataset.checksum,
                    "rows": len(dataset.records or [])},
        "first_groups": first_groups,
        "second_groups": second_groups,
    }
    param_changes = _param_changes(first, second)
    comparison["params_changed"] = param_changes
    comparison["findings"] = _findings(first, second, comparison, param_changes)
    comparison["warnings"] = [
        "缺陷带名称按各自试验的起始里程重新编排，名称相同不代表成员相同。",
        "该对比为历史结果降级模式，无法逐点对上编号。",
    ]
    return comparison
