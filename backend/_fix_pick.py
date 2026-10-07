from pathlib import Path
p = Path(r"services/media_pipeline.py")
t = p.read_text(encoding="utf-8")
a = t.find("def _uve_pick(")
b = t.find("def _uve_annotate(")
new = """def _uve_pick(scored, scenario, registry, used_keys, limit=3):
    pool = [c for c in scored if (c.get("verification") or {}).get("accepted")]
    status = "verified"
    if not pool:
        pool = [c for c in scored if float(c.get("universal_score") or 0) >= 0.05]
        status = "relaxed"
    if not pool:
        pool = list(scored[:8])
        status = "fallback"
    picked = []
    for candidate in pool:
        key = _uve_candidate_key(candidate)
        if not key or key in used_keys:
            continue
        try:
            allowed, _ = registry.can_use(url=key, title=result_title(candidate))
        except Exception:
            allowed = True
        if not allowed:
            continue
        item = dict(candidate)
        item["verification_status"] = status
        item["verification_score"] = item.get("universal_score")
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


"""
p.write_text(t[:a] + new + t[b:], encoding="utf-8")
print("OK")
