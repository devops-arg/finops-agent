"""
HTML Report Generator — full-dashboard, self-contained HTML cost report.

Sections:
  1. Header + Totals
  2. Executive Summary (KPIs, anomalies, top opportunities)
  3. Cost Overview (spend by service, SVG sparklines)
  4. Infrastructure Health (EC2, RDS, EKS, ElastiCache, …)
  5. Cleanup Findings
  6. Rightsizing Findings
  7. Billing Insights
  8. Cost Optimizer Recommendations
  9. DevOps ARG CTA + Footer
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

_SKIP_RESOURCE_TYPES = {"security_group"}
_SKIP_CATEGORIES = {"security"}
_DEDUPE_THRESHOLD = 3

_TYPE_LABEL = {
    "s3_bucket": "S3 Storage",
    "ebs_snapshot": "EBS Snapshot",
    "ebs_volume": "EBS Volume",
    "ecr_repository": "ECR Repository",
    "rds_instance": "RDS Instance",
    "rds_snapshot": "RDS Snapshot",
    "aurora_cluster": "Aurora Cluster",
    "ec2_instance": "EC2 Instance",
    "elastic_ip": "Elastic IP",
    "nat_gateway": "NAT Gateway",
    "load_balancer": "Load Balancer",
    "elasticache_cluster": "ElastiCache",
    "lambda_function": "Lambda",
    "route53_hosted_zone": "Route 53 Zone",
    "cloudfront_distribution": "CloudFront",
    "secret": "Secrets Manager",
}

CATEGORY_META = {
    "cost": ("💰", "Cost Analysis"),
    "networking": ("🌐", "Networking"),
    "commitments": ("🤝", "Commitments (RI/SP)"),
    "compute": ("⚡", "Compute"),
    "storage": ("💾", "Storage & Databases"),
    "observability": ("📡", "Observability"),
}

STATUS_COLOR = {
    "critical": "#ef4444",
    "warning": "#f59e0b",
    "info": "#3b82f6",
    "ok": "#10b981",
}

# Palette for service bars / sparklines
_SVC_COLORS = [
    "#f59e0b",
    "#06b6d4",
    "#8b5cf6",
    "#10b981",
    "#f97316",
    "#ec4899",
    "#14b8a6",
    "#6366f1",
    "#84cc16",
    "#ef4444",
    "#0ea5e9",
    "#a78bfa",
    "#fb923c",
    "#34d399",
    "#f43f5e",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:,.0f}"


def _fmt_annual(n: float) -> str:
    return _fmt(n * 12)


def _sev_color(sev: str) -> str:
    return {
        "critical": "#ef4444",
        "high": "#ef4444",
        "warning": "#f59e0b",
        "medium": "#f59e0b",
        "info": "#3b82f6",
        "low": "#3b82f6",
    }.get(sev, "#71717a")


def _sev_bg(sev: str) -> str:
    return {
        "critical": "rgba(239,68,68,.10)",
        "high": "rgba(239,68,68,.10)",
        "warning": "rgba(245,158,11,.10)",
        "medium": "rgba(245,158,11,.10)",
        "info": "rgba(59,130,246,.10)",
        "low": "rgba(59,130,246,.10)",
    }.get(sev, "rgba(113,113,122,.10)")


def _sev_label(sev: str) -> str:
    return {
        "critical": "High",
        "high": "High",
        "warning": "Medium",
        "medium": "Medium",
        "info": "Low",
        "low": "Low",
    }.get(sev, sev.title())


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f"font-size:11px;font-weight:600;color:{color};background:{bg};"
        f'border:1px solid {color}33;">{text.upper()}</span>'
    )


def _status_dot(status: str) -> str:
    c = {"healthy": "#10b981", "warning": "#f59e0b", "critical": "#ef4444", "ok": "#10b981"}.get(
        status, "#71717a"
    )
    label = {"healthy": "Healthy", "warning": "Warning", "critical": "Critical", "ok": "OK"}.get(
        status, status.title()
    )
    return (
        f'<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:{c}">'
        f'<span style="width:8px;height:8px;border-radius:50%;background:{c};flex-shrink:0"></span>{label}</span>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# SVG SPARKLINE  (inline, no external dependencies)
# ─────────────────────────────────────────────────────────────────────────────


def _svg_sparkline(values: list[float], color: str, width: int = 110, height: int = 36) -> str:
    if not values or len(values) < 2:
        return ""
    mn = min(values)
    mx = max(values) or 1
    rng = mx - mn or 1
    n = len(values)
    pad = 3
    pts = [
        (pad + i * (width - 2 * pad) / max(n - 1, 1), pad + (height - 2 * pad) * (1 - (v - mn) / rng))
        for i, v in enumerate(values)
    ]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    fill = f"0,{height} " + poly + f" {width},{height}"
    gid = color.replace("#", "g")
    last_x, last_y = pts[-1]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="display:block;overflow:visible">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.25"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0.02"/>'
        f"</linearGradient></defs>"
        f'<polygon points="{fill}" fill="url(#{gid})"/>'
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="{color}"/>'
        f"</svg>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────


def _deduplicate_findings(findings: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    solo: list[dict] = []

    for f in findings:
        rt = f.get("resource_type", "unknown")
        title = f.get("title", "")
        if f.get("estimated_savings_usd", 0) >= 50:
            solo.append(f)
            continue
        key = f"{rt}::{title[:40]}"
        groups[key].append(f)

    result = list(solo)
    for key, group in groups.items():
        if len(group) < _DEDUPE_THRESHOLD:
            result.extend(group)
        else:
            total_savings = sum(x.get("estimated_savings_usd", 0) for x in group)
            total_cost = sum(x.get("monthly_cost_usd", 0) for x in group)
            regions = sorted({x.get("region", "") for x in group if x.get("region")})
            sample = group[0]
            result.append(
                {
                    **sample,
                    "_is_group": True,
                    "_group_count": len(group),
                    "_group_members": group,
                    "estimated_savings_usd": total_savings,
                    "monthly_cost_usd": total_cost,
                    "region": ", ".join(regions[:3]) + ("…" if len(regions) > 3 else ""),
                    "resource_id": f"{len(group)} resources",
                    "fix_command": None,
                }
            )

    result.sort(key=lambda x: -x.get("estimated_savings_usd", 0))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# FINDINGS SECTION
# ─────────────────────────────────────────────────────────────────────────────

_GROUP_ROW_IDX = 0


def _finding_rows(f: dict, idx: int) -> str:
    global _GROUP_ROW_IDX
    sev = f.get("severity", "info")
    savings = f.get("estimated_savings_usd", 0)
    cost = f.get("monthly_cost_usd", 0)
    is_group = f.get("_is_group", False)
    count = f.get("_group_count", 1)
    fix_cmd = f.get("fix_command")
    rt = f.get("resource_type", "")
    svc_label = _TYPE_LABEL.get(rt, f.get("service", rt))
    rid = f.get("resource_id", "")
    region = f.get("region", "")
    title = f.get("title", "")
    desc = f.get("description", "")
    bg = "#1c1c1e" if idx % 2 == 0 else "#141416"

    if is_group:
        gid = f"grp-{_GROUP_ROW_IDX}"
        _GROUP_ROW_IDX += 1
        members = f.get("_group_members", [])
        resource_cell = (
            f'<span style="color:#a78bfa;font-weight:600;cursor:pointer;" '
            f"onclick=\"var r=document.getElementById('{gid}');"
            f"r.style.display=r.style.display==='none'?'table-row-group':'none';"
            f"this.textContent=r.style.display==='none'?'▶ {count} {svc_label} resources (click to expand)':'"
            f"▼ {count} {svc_label} resources (click to collapse)';\">"
            f"▶ {count} {svc_label} resources (click to expand)</span>"
        )
        cmd_cell = '<span style="color:#52525b;font-size:11px">expand to see per-resource commands</span>'

        member_rows = ""
        for m in members:
            m_rid = m.get("resource_id", "")
            m_region = m.get("region", "")
            m_cost = m.get("monthly_cost_usd", 0)
            m_sav = m.get("estimated_savings_usd", 0)
            m_cmd = m.get("fix_command", "")
            m_cmd_html = (
                (
                    f'<code style="display:block;background:#0a0a0f;color:#58a6ff;border-radius:3px;'
                    f"padding:3px 6px;font-size:10px;word-break:break-all;margin-top:3px;"
                    f'white-space:pre-wrap;">{m_cmd}</code>'
                )
                if m_cmd
                else ""
            )
            member_rows += f"""
            <tr style="background:#111114;border-bottom:1px solid #27272a;">
              <td style="padding:6px 14px 6px 28px;" colspan="2">
                <span style="font-family:monospace;font-size:11px;color:#a1a1aa">{m_rid}</span>
                {m_cmd_html}
              </td>
              <td style="padding:6px 14px;font-family:monospace;font-size:11px;color:#52525b">{m_region}</td>
              <td style="padding:6px 14px;text-align:right;font-family:monospace;font-size:11px;color:#71717a">{_fmt(m_cost)}</td>
              <td style="padding:6px 14px;text-align:right;font-family:monospace;font-size:11px;color:#10b981">{_fmt(m_sav)}</td>
            </tr>"""

        expand_block = (
            (
                f'<tbody id="{gid}" style="display:none">'
                f'<tr style="background:#0f0f12;"><td colspan="5" style="padding:4px 14px;">'
                f'<span style="font-size:10px;color:#52525b;font-weight:600;text-transform:uppercase;letter-spacing:.05em">All {count} resources</span>'
                f"</td></tr>" + member_rows + "</tbody>"
            )
            if members
            else ""
        )
    else:
        resource_cell = f'<span style="font-family:monospace;font-size:12px;color:#e2e8f0;word-break:break-all">{rid}</span>'
        gid = None
        expand_block = ""
        cmd_cell = (
            (
                f'<code style="display:block;background:#0a0a0f;color:#58a6ff;border-radius:4px;'
                f"padding:4px 8px;font-size:11px;word-break:break-all;margin-top:4px;"
                f'white-space:pre-wrap;">{fix_cmd}</code>'
            )
            if fix_cmd
            else ""
        )

    main_row = f"""
    <tr style="background:{bg};border-bottom:1px solid #27272a;">
      <td style="padding:10px 14px;vertical-align:top;white-space:nowrap">
        {_badge(_sev_label(sev), _sev_color(sev), _sev_bg(sev))}
      </td>
      <td style="padding:10px 14px;vertical-align:top;min-width:260px">
        <div style="font-size:13px;font-weight:600;color:#fafafa;margin-bottom:3px">{title}</div>
        <div style="font-size:12px;color:#a1a1aa;line-height:1.5">{desc}</div>
        {cmd_cell}
      </td>
      <td style="padding:10px 14px;vertical-align:top">{resource_cell}</td>
      <td style="padding:10px 14px;vertical-align:top;white-space:nowrap">
        <span style="font-family:monospace;font-size:12px;color:#71717a">{region}</span>
      </td>
      <td style="padding:10px 14px;vertical-align:top;text-align:right;white-space:nowrap">
        <span style="font-size:13px;color:#fafafa;font-family:monospace">{_fmt(cost)}</span>
        <div style="font-size:10px;color:#71717a">/mo</div>
      </td>
      <td style="padding:10px 14px;vertical-align:top;text-align:right;white-space:nowrap">
        <span style="font-size:14px;font-weight:700;color:#10b981;font-family:monospace">{_fmt(savings)}</span>
        <div style="font-size:10px;color:#71717a">/mo</div>
      </td>
    </tr>"""

    return main_row + expand_block


def _findings_section(title: str, icon: str, findings: list[dict]) -> str:
    global _GROUP_ROW_IDX
    _GROUP_ROW_IDX = 0
    if not findings:
        return ""
    rows = "".join(_finding_rows(f, i) for i, f in enumerate(findings))
    total_savings = sum(f.get("estimated_savings_usd", 0) for f in findings)
    total_cost = sum(f.get("monthly_cost_usd", 0) for f in findings)
    return f"""
  <div class="section">
    <div class="section-header">
      <span style="font-size:22px">{icon}</span>
      <div>
        <div class="section-title">{title}</div>
        <div class="section-sub">{len(findings)} findings · current cost {_fmt(total_cost)}/mo · savings {_fmt(total_savings)}/mo</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:24px;font-weight:700;color:#10b981;font-family:monospace">{_fmt(total_savings)}</div>
        <div style="font-size:11px;color:#71717a">/mo identifiable savings</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:2px">{_fmt_annual(total_savings)}/year</div>
      </div>
    </div>
    <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
    <table style="width:100%;border-collapse:collapse;font-family:'Inter',sans-serif;table-layout:fixed">
      <colgroup>
        <col style="width:88px">
        <col style="width:auto">
        <col style="width:180px">
        <col style="width:100px">
        <col style="width:80px">
        <col style="width:90px">
      </colgroup>
      <thead>
        <tr style="background:#18181b;border-bottom:2px solid #3f3f46;">
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Priority</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Finding</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Resource</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Region</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Cost</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Saves</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
  </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# SERVICES BREAKDOWN SECTION
# ─────────────────────────────────────────────────────────────────────────────


def _services_section(trend_data: Optional[dict]) -> str:
    if not trend_data:
        return ""
    by_service = trend_data.get("byService") or []
    period = trend_data.get("period", "1m")
    if not by_service:
        return ""

    total_cost = sum(s.get("cost", 0) for s in by_service)
    period_label = {"3d": "3 Days", "1w": "7 Days", "1m": "30 Days", "3m": "3 Months", "1y": "1 Year"}.get(
        period, period
    )

    rows_html = ""
    for idx, svc in enumerate(by_service[:20]):
        color = _SVC_COLORS[idx % len(_SVC_COLORS)]
        cost = svc.get("cost", 0)
        daily = svc.get("daily_avg") or (cost / 30)
        pct = (cost / total_cost * 100) if total_cost else 0
        tl = svc.get("timeline", [])
        values = [p.get("cost", 0) for p in tl]
        spark = _svg_sparkline(values, color) if len(values) >= 2 else ""

        # Trend direction
        trend_html = ""
        if len(values) >= 2:
            half = len(values) // 2
            first_h = sum(values[:half]) or 1
            second_h = sum(values[half:])
            delta = (second_h - first_h) / first_h * 100
            arrow = "▲" if delta > 0 else "▼"
            clr = "#ef4444" if delta > 0 else "#10b981"
            trend_html = (
                f'<span style="color:{clr};font-weight:600;font-size:12px">{arrow}{abs(delta):.1f}%</span>'
            )

        bar_pct = min(pct, 100)
        rows_html += f"""
        <tr style="border-bottom:1px solid #27272a;">
          <td style="padding:10px 14px;white-space:nowrap">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="width:10px;height:10px;border-radius:50%;background:{color};flex-shrink:0"></span>
              <span style="font-size:13px;font-weight:600;color:#fafafa">{svc.get('name','')}</span>
            </div>
          </td>
          <td style="padding:10px 14px;text-align:right;white-space:nowrap">
            <span style="font-size:14px;font-weight:700;color:{color};font-family:monospace">{_fmt(cost)}</span>
          </td>
          <td style="padding:10px 14px;text-align:right;white-space:nowrap">
            <span style="font-size:12px;color:#a1a1aa;font-family:monospace">{_fmt(daily)}/day</span>
          </td>
          <td style="padding:10px 14px;">
            <div style="display:flex;align-items:center;gap:8px">
              <div style="flex:1;background:#27272a;border-radius:3px;height:6px;min-width:80px">
                <div style="width:{bar_pct:.1f}%;background:{color};height:6px;border-radius:3px"></div>
              </div>
              <span style="font-size:11px;color:#71717a;width:36px;text-align:right">{pct:.1f}%</span>
            </div>
          </td>
          <td style="padding:10px 14px">{trend_html}</td>
          <td style="padding:10px 14px;text-align:right">{spark}</td>
        </tr>"""

    return f"""
  <div class="section">
    <div class="section-header">
      <span style="font-size:22px">📈</span>
      <div>
        <div class="section-title">Cost by Service</div>
        <div class="section-sub">Period: {period_label} · {len(by_service)} services · total {_fmt(total_cost)}</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:24px;font-weight:700;color:#fafafa;font-family:monospace">{_fmt(total_cost)}</div>
        <div style="font-size:11px;color:#71717a">total spend · {period_label}</div>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-family:'Inter',sans-serif">
      <thead>
        <tr style="background:#18181b;border-bottom:2px solid #3f3f46;">
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Service</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Total</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Daily Avg</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Share</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Trend</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Sparkline</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>
  </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# INFRASTRUCTURE SECTION
# ─────────────────────────────────────────────────────────────────────────────


def _infra_kv(label: str, value: Any, highlight: bool = False) -> str:
    val_style = "color:#fafafa;font-weight:600" if highlight else "color:#a1a1aa"
    return (
        f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
        f'border-bottom:1px solid #1f1f22;">'
        f'<span style="font-size:12px;color:#71717a">{label}</span>'
        f'<span style="font-size:12px;{val_style}">{value}</span>'
        f"</div>"
    )


def _infra_card(icon: str, title: str, status: str, monthly_cost: float, rows: str, detail: str = "") -> str:
    return f"""
    <div style="background:#18181b;border:1px solid #27272a;border-radius:8px;padding:18px;display:flex;flex-direction:column;gap:10px;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:20px">{icon}</span>
          <span style="font-size:15px;font-weight:700;color:#fafafa">{title}</span>
        </div>
        {_status_dot(status)}
      </div>
      {f'<div style="font-size:12px;color:#71717a;line-height:1.5">{detail}</div>' if detail else ''}
      <div style="border-top:1px solid #27272a;padding-top:10px">{rows}</div>
      <div style="margin-top:auto;padding-top:8px;border-top:1px solid #27272a;display:flex;align-items:center;justify-content:space-between;">
        <span style="font-size:11px;color:#71717a">Monthly cost</span>
        <span style="font-size:16px;font-weight:700;color:#fafafa;font-family:monospace">{_fmt(monthly_cost)}</span>
      </div>
    </div>"""


def _infrastructure_section(infra_data: Optional[dict]) -> str:
    if not infra_data:
        return ""
    resources = infra_data.get("resources") or infra_data  # handle both shapes
    if not resources or not isinstance(resources, dict):
        return ""

    cards_html = ""

    # EC2
    ec2 = resources.get("ec2") or {}
    if ec2:
        rows = (
            _infra_kv("Running / Stopped", f"{ec2.get('running',0)} / {ec2.get('stopped',0)}")
            + _infra_kv("Avg CPU", f"{ec2.get('avg_cpu_pct',0):.1f}%", ec2.get("avg_cpu_pct", 0) < 30)
            + _infra_kv("Spot coverage", f"{ec2.get('spot_coverage_pct',0)}%")
            + _infra_kv("Graviton", f"{ec2.get('graviton_coverage_pct',0)}%")
        )
        warning = ec2.get("warning") or ec2.get("detail", "")
        cards_html += _infra_card(
            "🖥️", "EC2 Compute", ec2.get("status", "ok"), ec2.get("monthly_cost", 0), rows, warning
        )

    # RDS
    rds = resources.get("rds") or {}
    if rds:
        rows = (
            _infra_kv("Clusters / Instances", f"{rds.get('clusters',0)} / {rds.get('instances',0)}")
            + _infra_kv("Engine", rds.get("engine", "—"))
            + _infra_kv("Avg CPU", f"{rds.get('avg_cpu_pct',0):.1f}%")
            + _infra_kv(
                "Connections", f"{rds.get('connections_active',0):,} / {rds.get('connections_max',0):,}"
            )
            + _infra_kv("Storage", f"{rds.get('storage_used_gb',0)} / {rds.get('storage_allocated_gb',0)} GB")
        )
        cards_html += _infra_card(
            "🗄️",
            "RDS Databases",
            rds.get("status", "ok"),
            rds.get("monthly_cost", 0),
            rows,
            rds.get("warning") or rds.get("detail", ""),
        )

    # EKS
    eks = resources.get("eks") or {}
    if eks:
        node_str = ", ".join(f"{e}: {n}" for e, n in (eks.get("nodes") or {}).items())
        rows = (
            _infra_kv("Clusters", eks.get("clusters", 0))
            + _infra_kv("Nodes", node_str or str(sum((eks.get("nodes") or {}).values())))
            + _infra_kv("Pods running / failed", f"{eks.get('pods_running',0)} / {eks.get('pods_failed',0)}")
            + _infra_kv("Avg CPU", f"{eks.get('avg_cpu_pct',0):.1f}%")
            + _infra_kv("Provisioner", eks.get("node_provisioner", "—"))
        )
        cards_html += _infra_card(
            "☸️",
            "EKS Kubernetes",
            eks.get("status", "ok"),
            eks.get("monthly_cost", 0),
            rows,
            eks.get("detail", ""),
        )

    # ElastiCache
    ec = resources.get("elasticache") or {}
    if ec:
        rows = (
            _infra_kv("Clusters / Nodes", f"{ec.get('clusters',0)} / {ec.get('total_nodes',0)}")
            + _infra_kv("Engine", ec.get("engine", "—"))
            + _infra_kv("Hit rate", f"{ec.get('hit_rate_pct',0):.1f}%", True)
            + _infra_kv("Memory", f"{ec.get('memory_used_gb',0):.1f} / {ec.get('memory_total_gb',0):.1f} GB")
            + _infra_kv("Evictions/s", str(ec.get("evictions_per_sec", 0)))
        )
        cards_html += _infra_card(
            "⚡",
            "ElastiCache / Redis",
            ec.get("status", "ok"),
            ec.get("monthly_cost", 0),
            rows,
            ec.get("detail", ""),
        )

    # OpenSearch
    os_ = resources.get("opensearch") or {}
    if os_:
        rows = (
            _infra_kv("Domains / Nodes", f"{os_.get('domains',0)} / {os_.get('nodes',0)}")
            + _infra_kv("Engine", os_.get("engine_version", "—"))
            + _infra_kv("Storage", f"{os_.get('storage_used_gb',0)} / {os_.get('storage_total_gb',0)} GB")
            + _infra_kv("Avg CPU", f"{os_.get('avg_cpu_pct',0):.1f}%")
        )
        cards_html += _infra_card(
            "🔍",
            "OpenSearch",
            os_.get("status", "ok"),
            os_.get("monthly_cost", 0),
            rows,
            os_.get("detail", ""),
        )

    # S3
    s3 = resources.get("s3") or {}
    if s3:
        rows = (
            _infra_kv("Buckets", s3.get("buckets", 0))
            + _infra_kv("Total size", s3.get("total_size", "—"))
            + _infra_kv("Versioning enabled", s3.get("versioning_enabled", 0))
            + _infra_kv("Lifecycle policies", s3.get("lifecycle_policies", 0))
        )
        cards_html += _infra_card(
            "🪣", "S3 Storage", s3.get("status", "ok"), s3.get("monthly_cost", 0), rows, s3.get("detail", "")
        )

    # CloudFront
    cf = resources.get("cloudfront") or {}
    if cf:
        rows = (
            _infra_kv("Distributions", cf.get("distributions", 0))
            + _infra_kv("Requests/mo", f"{cf.get('requests_monthly','—')}")
            + _infra_kv("Cache hit rate", f"{cf.get('cache_hit_pct',0):.1f}%", True)
            + _infra_kv("Data transferred", cf.get("data_transferred", "—"))
        )
        cards_html += _infra_card(
            "🌐",
            "CloudFront CDN",
            cf.get("status", "ok"),
            cf.get("monthly_cost", 0),
            rows,
            cf.get("detail", ""),
        )

    # MSK
    msk = resources.get("msk") or {}
    if msk:
        rows = (
            _infra_kv("Clusters", msk.get("clusters", 0))
            + _infra_kv("Brokers / partition", f"{msk.get('brokers',0)} / {msk.get('partitions',0)}")
            + _infra_kv("Engine", msk.get("engine_version", "—"))
        )
        cards_html += _infra_card(
            "📨",
            "MSK Kafka",
            msk.get("status", "ok"),
            msk.get("monthly_cost", 0),
            rows,
            msk.get("detail", ""),
        )

    if not cards_html:
        return ""

    total_infra = sum(
        (resources.get(k) or {}).get("monthly_cost", 0)
        for k in ("ec2", "rds", "eks", "elasticache", "opensearch", "s3", "cloudfront", "msk")
    )

    return f"""
  <div class="section">
    <div class="section-header">
      <span style="font-size:22px">🏗️</span>
      <div>
        <div class="section-title">Infrastructure Health</div>
        <div class="section-sub">Live resource summary — EC2, RDS, EKS, ElastiCache, S3 &amp; more</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:24px;font-weight:700;color:#fafafa;font-family:monospace">{_fmt(total_infra)}</div>
        <div style="font-size:11px;color:#71717a">/mo total infrastructure</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;">
      {cards_html}
    </div>
  </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMIZER SECTION
# ─────────────────────────────────────────────────────────────────────────────

_OPT_TYPE_ICON = {
    "rightsizing": "⚡",
    "commitment": "🤝",
    "networking": "🌐",
    "storage": "💾",
    "compute": "🖥️",
    "licensing": "📄",
}


def _optimizer_section(optimize_data: Optional[dict]) -> str:
    if not optimize_data:
        return ""
    recs = optimize_data.get("recommendations") or []
    if not recs:
        return ""

    total_monthly = sum(r.get("monthly_savings", 0) for r in recs)
    total_annual = sum(r.get("annual_savings", 0) for r in recs)

    rows_html = ""
    for idx, rec in enumerate(recs):
        sev = rec.get("severity", "medium")
        sav_m = rec.get("monthly_savings", 0)
        sav_a = rec.get("annual_savings", 0)
        effort = rec.get("effort", "medium").title()
        risk = rec.get("risk", "medium").title()
        typ = rec.get("type", "rightsizing")
        icon = _OPT_TYPE_ICON.get(typ, "💡")
        impl = rec.get("implementation", "")
        bg = "#1c1c1e" if idx % 2 == 0 else "#141416"
        effort_color = {"Low": "#10b981", "Medium": "#f59e0b", "High": "#ef4444"}.get(effort, "#71717a")
        risk_color = {"Low": "#10b981", "Medium": "#f59e0b", "High": "#ef4444"}.get(risk, "#71717a")

        impl_html = (
            (
                f'<code style="display:block;background:#0a0a0f;color:#58a6ff;border-radius:4px;'
                f"padding:4px 8px;font-size:11px;word-break:break-all;margin-top:6px;"
                f'white-space:pre-wrap;">{impl}</code>'
            )
            if impl
            else ""
        )

        rows_html += f"""
        <tr style="background:{bg};border-bottom:1px solid #27272a;">
          <td style="padding:10px 14px;vertical-align:top;white-space:nowrap">
            <div style="font-size:18px;margin-bottom:4px">{icon}</div>
            {_badge(_sev_label(sev), _sev_color(sev), _sev_bg(sev))}
          </td>
          <td style="padding:10px 14px;vertical-align:top">
            <div style="font-size:13px;font-weight:600;color:#fafafa;margin-bottom:4px">{rec.get('title','')}</div>
            <div style="font-size:12px;color:#a1a1aa;line-height:1.5">{rec.get('detail','')}</div>
            {impl_html}
          </td>
          <td style="padding:10px 14px;vertical-align:top;white-space:nowrap">
            <span style="font-size:12px;color:#71717a">{rec.get('service','')}</span>
          </td>
          <td style="padding:10px 14px;vertical-align:top;white-space:nowrap;text-align:center">
            <span style="font-size:12px;font-weight:600;color:{effort_color}">{effort}</span>
          </td>
          <td style="padding:10px 14px;vertical-align:top;white-space:nowrap;text-align:center">
            <span style="font-size:12px;font-weight:600;color:{risk_color}">{risk}</span>
          </td>
          <td style="padding:10px 14px;vertical-align:top;text-align:right;white-space:nowrap">
            <div style="font-size:16px;font-weight:700;color:#10b981;font-family:monospace">{_fmt(sav_m)}</div>
            <div style="font-size:11px;color:#71717a">/mo</div>
            <div style="font-size:11px;color:#52525b;margin-top:2px">{_fmt(sav_a)}/yr</div>
          </td>
        </tr>"""

    return f"""
  <div class="section">
    <div class="section-header">
      <span style="font-size:22px">🎯</span>
      <div>
        <div class="section-title">Cost Optimizer Recommendations</div>
        <div class="section-sub">{len(recs)} recommendations · rightsizing, commitments, networking &amp; more</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:24px;font-weight:700;color:#10b981;font-family:monospace">{_fmt(total_monthly)}</div>
        <div style="font-size:11px;color:#71717a">/mo identifiable savings</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:2px">{_fmt(total_annual)}/year</div>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-family:'Inter',sans-serif;table-layout:fixed">
      <colgroup>
        <col style="width:80px">
        <col style="width:auto">
        <col style="width:140px">
        <col style="width:70px">
        <col style="width:60px">
        <col style="width:90px">
      </colgroup>
      <thead>
        <tr style="background:#18181b;border-bottom:2px solid #3f3f46;">
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Type</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Recommendation</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Service</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Effort</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Risk</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#71717a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Saves</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>
  </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# INSIGHTS SECTION
# ─────────────────────────────────────────────────────────────────────────────


def _insight_card(ins: dict, idx: int) -> str:
    status = ins.get("status", "info")
    color = STATUS_COLOR.get(status, "#71717a")
    savings = ins.get("savings_usd", 0)
    bg = "#1a1a1a" if idx % 2 == 0 else "#141414"
    savings_html = (
        (
            f'<span style="font-size:13px;font-weight:700;color:#10b981;font-family:monospace">'
            f"{_fmt(savings)}/mo</span>"
        )
        if savings > 0
        else ""
    )

    return f"""
    <div style="background:{bg};border:1px solid #27272a;border-left:3px solid {color};
                border-radius:6px;padding:14px 18px;margin-bottom:10px;">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:6px;">
        <div>
          {_badge(status, color, color + "18")}
          <span style="margin-left:8px;font-size:14px;font-weight:600;color:#fafafa">{ins.get('title','')}</span>
        </div>
        <div style="flex-shrink:0;font-size:18px;font-weight:700;color:#fafafa;font-family:monospace">{ins.get('value','')}</div>
      </div>
      <div style="font-size:12px;color:#a1a1aa;line-height:1.5;margin-bottom:6px">{ins.get('detail','')}</div>
      <div style="font-size:12px;color:#71717a;background:#111;border-radius:4px;padding:6px 10px;">
        <span style="color:#C9A227;font-weight:600">→ </span>{ins.get('recommendation','')}
      </div>
      {f'<div style="margin-top:8px">{savings_html}<span style="font-size:11px;color:#71717a;margin-left:6px">{_fmt_annual(savings)}/year potential</span></div>' if savings > 0 else ""}
    </div>"""


def _insights_section(insights: list[dict]) -> str:
    if not insights:
        return ""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for i in insights:
        by_cat[i.get("category", "cost")].append(i)

    cat_order = ["cost", "networking", "commitments", "compute", "storage", "observability"]
    sections_html = ""
    for cat in cat_order:
        items = by_cat.get(cat)
        if not items:
            continue
        icon, label = CATEGORY_META.get(cat, ("📊", cat.title()))
        cards = "".join(_insight_card(i, j) for j, i in enumerate(items))
        sections_html += f"""
    <div style="margin-bottom:28px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;
                  padding-bottom:8px;border-bottom:1px solid #27272a;">
        <span style="font-size:16px">{icon}</span>
        <span style="font-size:12px;font-weight:600;color:#a1a1aa;text-transform:uppercase;letter-spacing:.05em">{label}</span>
        <span style="font-size:11px;color:#71717a;background:#18181b;border:1px solid #27272a;
                     border-radius:10px;padding:1px 7px">{len(items)}</span>
      </div>
      {cards}
    </div>"""

    total_savings = sum(i.get("savings_usd", 0) for i in insights)
    return f"""
  <div class="section">
    <div class="section-header">
      <span style="font-size:22px">📊</span>
      <div>
        <div class="section-title">Billing Insights</div>
        <div class="section-sub">{len(insights)} automated checks · no LLM required</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:24px;font-weight:700;color:#10b981;font-family:monospace">{_fmt(total_savings)}</div>
        <div style="font-size:11px;color:#71717a">/mo identifiable savings</div>
      </div>
    </div>
    {sections_html}
  </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATOR
# ─────────────────────────────────────────────────────────────────────────────


def generate_html_report(
    findings: list[dict],
    insights: list[dict],
    account_label: str = "AWS Account",
    generated_at: Optional[str] = None,
    report_data: Optional[dict] = None,  # from /api/report
    trend_data: Optional[dict] = None,  # from /api/report/trend
    infra_data: Optional[dict] = None,  # from /api/infrastructure
    optimize_data: Optional[dict] = None,  # from /api/optimize
) -> str:
    if generated_at is None:
        generated_at = datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")

    # ── Filter ───────────────────────────────────────────────────────────────
    filtered = [
        f
        for f in findings
        if f.get("resource_type") not in _SKIP_RESOURCE_TYPES
        and f.get("category") not in _SKIP_CATEGORIES
        and f.get("estimated_savings_usd", 0) > 0
    ]
    insight_filtered = [
        i for i in insights if i.get("category") not in _SKIP_CATEGORIES and i.get("status") != "ok"
    ]

    # ── Split cleanup vs rightsize ───────────────────────────────────────────
    cleanup = _deduplicate_findings([f for f in filtered if f.get("category") == "cleanup"])
    rightsize = _deduplicate_findings(
        [f for f in filtered if f.get("category") in ("rightsize", "rightsizing")]
    )

    # ── Totals ───────────────────────────────────────────────────────────────
    total_savings = sum(f.get("estimated_savings_usd", 0) for f in filtered)
    insight_savings = sum(i.get("savings_usd", 0) for i in insight_filtered)
    opt_savings = sum(r.get("monthly_savings", 0) for r in (optimize_data or {}).get("recommendations", []))
    combined_savings = total_savings + insight_savings
    critical_count = sum(1 for f in filtered if f.get("severity") == "critical")
    warning_count = sum(1 for f in filtered if f.get("severity") == "warning")
    ins_critical = sum(1 for i in insight_filtered if i.get("status") == "critical")
    ins_warning = sum(1 for i in insight_filtered if i.get("status") == "warning")

    # ── Cost overview from report_data ───────────────────────────────────────
    total_spend = 0.0
    daily_avg = 0.0
    top_service = "—"
    anomaly_count = 0
    if report_data:
        total_spend = report_data.get("totalLastWeek") or report_data.get("total_spend") or 0
        daily_avg = report_data.get("dailyAverage") or report_data.get("daily_avg") or 0
        by_svc = report_data.get("byService") or []
        if by_svc:
            top = max(by_svc, key=lambda s: s.get("lastWeek") or s.get("cost") or 0, default={})
            top_service = top.get("name", "—")
        anomalies = report_data.get("anomalies") or []
        anomaly_count = len([a for a in anomalies if a.get("status") == "OPEN"])

    # ── Top opportunities ────────────────────────────────────────────────────
    top3 = sorted(
        filtered
        + [
            {
                "title": i["title"],
                "estimated_savings_usd": i.get("savings_usd", 0),
                "severity": i.get("status", "info"),
                "resource_type": "insight",
            }
            for i in insight_filtered
            if i.get("savings_usd", 0) > 0
        ],
        key=lambda x: -x.get("estimated_savings_usd", 0),
    )[:3]

    top3_html = ""
    for item in top3:
        sav = item.get("estimated_savings_usd", 0)
        sev = item.get("severity", "info")
        top3_html += f"""
        <div style="background:#18181b;border:1px solid #27272a;border-radius:6px;padding:14px 18px;">
          {_badge(_sev_label(sev), _sev_color(sev), _sev_bg(sev))}
          <div style="font-size:14px;font-weight:600;color:#fafafa;margin-top:6px">{item.get('title','')}</div>
          <div style="font-size:22px;font-weight:700;color:#10b981;font-family:monospace;margin-top:6px">{_fmt(sav)}<span style="font-size:13px;color:#71717a">/mo</span></div>
          <div style="font-size:11px;color:#a1a1aa;margin-top:2px">{_fmt_annual(sav)}/year</div>
        </div>"""

    # ── Anomalies row ────────────────────────────────────────────────────────
    anomaly_html = ""
    if report_data and report_data.get("anomalies"):
        anomalies = report_data["anomalies"]
        cards = ""
        for a in anomalies[:6]:
            impact = a.get("total_impact") or a.get("impact") or a.get("DimensionalValue") or 0
            try:
                impact = float(impact)
            except:
                impact = 0
            svc = a.get("service") or a.get("rootCauses", "—")
            region = a.get("region", "")
            status = a.get("status", "OPEN")
            sc = "#ef4444" if status == "OPEN" else "#10b981"
            cards += f"""
            <div style="background:#18181b;border:1px solid #27272a;border-left:3px solid {sc};border-radius:6px;padding:12px 16px;">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:4px">
                <span style="font-size:13px;font-weight:600;color:#fafafa">{svc}</span>
                <span style="font-size:12px;font-weight:700;color:#ef4444;font-family:monospace">{_fmt(impact)}</span>
              </div>
              <div style="font-size:11px;color:#71717a">{region or status}</div>
            </div>"""
        if cards:
            anomaly_html = f"""
    <div style="margin-top:24px">
      <div style="font-size:12px;font-weight:600;color:#71717a;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px;">
        🚨 Cost Anomalies ({len(anomalies)})
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">{cards}</div>
    </div>"""

    # ── HTML sections ────────────────────────────────────────────────────────
    services_section = _services_section(trend_data)
    infra_section = _infrastructure_section(infra_data)
    cleanup_section = _findings_section("Cleanup — Resources to Delete", "🗑️", cleanup)
    rightsize_section = _findings_section("Rightsizing — Resources to Optimize", "⚡", rightsize)
    optimizer_section = _optimizer_section(optimize_data)
    insights_section = _insights_section(insight_filtered)

    # ── Table of Contents ────────────────────────────────────────────────────
    toc_items = [
        ("📋", "Executive Summary"),
    ]
    if services_section:
        toc_items.append(("📈", "Cost by Service"))
    if infra_section:
        toc_items.append(("🏗️", "Infrastructure Health"))
    if cleanup_section:
        toc_items.append(("🗑️", f"Cleanup ({len(cleanup)} findings)"))
    if rightsize_section:
        toc_items.append(("⚡", f"Rightsizing ({len(rightsize)} findings)"))
    if insights_section:
        toc_items.append(("📊", f"Billing Insights ({len(insight_filtered)})"))
    if optimizer_section:
        toc_items.append(("🎯", "Cost Optimizer"))

    toc_html = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;padding:6px 14px;'
        f"background:#18181b;border:1px solid #27272a;border-radius:20px;font-size:12px;"
        f'color:#a1a1aa;white-space:nowrap">{icon} {label}</span>'
        for icon, label in toc_items
    )

    # ── Assemble ─────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FinOps Cost Report — {account_label}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:#0a0a0b;color:#fafafa;font-family:'Inter',system-ui,sans-serif;font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased;}}
  .page{{max-width:1200px;margin:0 auto;padding:40px 32px 80px;}}
  .section{{background:#121214;border:1px solid #27272a;border-radius:10px;padding:28px;margin-bottom:28px;}}
  .section-header{{display:flex;align-items:flex-start;gap:14px;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #27272a;}}
  .section-title{{font-size:18px;font-weight:700;color:#fafafa;letter-spacing:-0.02em;}}
  .section-sub{{font-size:12px;color:#71717a;margin-top:4px;}}
  @media print{{
    body{{background:#fff;color:#000;}}
    .section{{background:#fff;border-color:#e2e8f0;page-break-inside:avoid;}}
    .no-print{{display:none!important;}}
  }}
</style>
</head>
<body>
<div class="page">

  <!-- HEADER -->
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;padding-bottom:24px;border-bottom:2px solid #27272a;">
    <div style="display:flex;align-items:center;gap:16px;">
      <img src="https://www.devopsarg.com/devopsarg-logo.webp?v=fb8af88" alt="DevOps ARG"
           style="width:64px;height:64px;border-radius:12px;border:1px solid #27272a;" />
      <div>
        <div style="font-size:11px;color:#C9A227;font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px;">DevOps ARG · FinOps Intelligence Platform</div>
        <div style="font-size:28px;font-weight:800;color:#fafafa;letter-spacing:-0.03em;line-height:1.1;">AWS Cost Optimization Report</div>
        <div style="font-size:13px;color:#71717a;margin-top:6px;">{account_label} · Generated {generated_at}</div>
      </div>
    </div>
    <div style="text-align:right;flex-shrink:0;">
      <div style="font-size:11px;color:#71717a;margin-bottom:4px;">Total identifiable savings</div>
      <div style="font-size:42px;font-weight:800;color:#10b981;font-family:'JetBrains Mono',monospace;letter-spacing:-0.02em;">{_fmt(combined_savings)}</div>
      <div style="font-size:14px;color:#a1a1aa;margin-top:2px;">per month · {_fmt_annual(combined_savings)}/year</div>
    </div>
  </div>

  <!-- TABLE OF CONTENTS -->
  <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:28px;">
    {toc_html}
  </div>

  <!-- EXECUTIVE SUMMARY -->
  <div class="section">
    <div class="section-header">
      <span style="font-size:22px">📋</span>
      <div>
        <div class="section-title">Executive Summary</div>
        <div class="section-sub">Automated scan across EC2, RDS, EBS, S3, ECR, Lambda, networking &amp; commitments</div>
      </div>
    </div>

    <!-- KPI row -->
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px;margin-bottom:{'28' if not top3 else '20'}px;">
      {"" if not total_spend else f'''
      <div style="background:#18181b;border:1px solid #27272a;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Weekly Spend</div>
        <div style="font-size:26px;font-weight:700;color:#fafafa;font-family:monospace">{_fmt(total_spend)}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">{_fmt(daily_avg)}/day avg</div>
      </div>'''}
      {"" if not top_service or top_service == "—" else f'''
      <div style="background:#18181b;border:1px solid #27272a;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Top Service</div>
        <div style="font-size:15px;font-weight:700;color:#fafafa;line-height:1.3">{top_service}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">highest cost driver</div>
      </div>'''}
      <div style="background:#18181b;border:1px solid #27272a;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Waste Findings</div>
        <div style="font-size:26px;font-weight:700;color:#fafafa;font-family:monospace">{len(filtered)}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">resources to act on</div>
      </div>
      <div style="background:#18181b;border:1px solid #ef444433;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">High Priority</div>
        <div style="font-size:26px;font-weight:700;color:#ef4444;font-family:monospace">{critical_count + ins_critical}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">immediate action</div>
      </div>
      <div style="background:#18181b;border:1px solid #10b98133;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Waste Savings</div>
        <div style="font-size:26px;font-weight:700;color:#10b981;font-family:monospace">{_fmt(total_savings)}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">/mo from cleanup</div>
      </div>
      <div style="background:#18181b;border:1px solid #C9A22733;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Insights Savings</div>
        <div style="font-size:26px;font-weight:700;color:#C9A227;font-family:monospace">{_fmt(insight_savings)}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">/mo from optimizations</div>
      </div>
      {"" if not opt_savings else f'''
      <div style="background:#18181b;border:1px solid #6366f133;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Optimizer Savings</div>
        <div style="font-size:26px;font-weight:700;color:#6366f1;font-family:monospace">{_fmt(opt_savings)}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">/mo from recommendations</div>
      </div>'''}
      {"" if not anomaly_count else f'''
      <div style="background:#18181b;border:1px solid #ef444433;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;color:#71717a;margin-bottom:8px;">Open Anomalies</div>
        <div style="font-size:26px;font-weight:700;color:#ef4444;font-family:monospace">{anomaly_count}</div>
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px;">detected anomalies</div>
      </div>'''}
    </div>

    {f'''<!-- Top 3 opportunities -->
    <div style="font-size:12px;font-weight:600;color:#71717a;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px;">Top 3 Savings Opportunities</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;">
      {top3_html}
    </div>''' if top3_html else ""}

    {anomaly_html}
  </div>

  <!-- SERVICES SECTION -->
  {services_section}

  <!-- INFRASTRUCTURE SECTION -->
  {infra_section}

  <!-- CLEANUP SECTION -->
  {cleanup_section}

  <!-- RIGHTSIZING SECTION -->
  {rightsize_section}

  <!-- BILLING INSIGHTS SECTION -->
  {insights_section}

  <!-- OPTIMIZER SECTION -->
  {optimizer_section}

  <!-- DEVOPS ARG CTA -->
  <div style="background:linear-gradient(135deg,rgba(201,162,39,.12),rgba(201,162,39,.04));
              border:1px solid rgba(201,162,39,.3);border-radius:10px;padding:32px;
              display:flex;align-items:center;justify-content:space-between;gap:24px;">
    <div style="display:flex;align-items:center;gap:16px;">
      <img src="https://www.devopsarg.com/devopsarg-logo.webp?v=fb8af88" alt="DevOps ARG"
           style="width:64px;height:64px;border-radius:10px;border:1px solid rgba(201,162,39,.3);" />
      <div>
        <div style="font-size:18px;font-weight:700;color:#C9A227;margin-bottom:6px;">Let DevOps ARG implement these savings for you</div>
        <div style="font-size:13px;color:#a1a1aa;line-height:1.5;max-width:520px;">
          We implement these optimizations in &lt;2 weeks — zero downtime, safety snapshots before every deletion,
          post-change monitoring, and a written rollback plan. We help you cut cloud costs and scale with confidence.
        </div>
      </div>
    </div>
    <div style="flex-shrink:0;text-align:center;">
      <div style="font-size:32px;font-weight:800;color:#10b981;font-family:monospace;margin-bottom:4px;">{_fmt(combined_savings)}/mo</div>
      <div style="font-size:12px;color:#71717a;margin-bottom:14px;">identified savings</div>
      <a href="https://www.devopsarg.com/en/#contact" target="_blank"
         style="display:block;background:#C9A227;color:#0a0a0b;font-weight:700;
                font-size:13px;padding:10px 24px;border-radius:6px;text-decoration:none;margin-bottom:8px;">
        Contact us → devopsarg.com
      </a>
      <div style="display:flex;flex-direction:column;gap:6px;align-items:center;">
        <a href="https://www.devopsarg.com/en/blog/aws-cost-optimization-case-study/" target="_blank"
           style="font-size:12px;color:#C9A227;text-decoration:none;opacity:.8;">📄 AWS cost optimization case study</a>
        <a href="https://www.devopsarg.com/en/blog/karpenter-spot-scale-to-zero/" target="_blank"
           style="font-size:12px;color:#C9A227;text-decoration:none;opacity:.8;">⚡ Karpenter + Spot migrations</a>
        <a href="https://www.devopsarg.com/en/blog/" target="_blank"
           style="font-size:12px;color:#C9A227;text-decoration:none;opacity:.8;">📝 All case studies &amp; blog</a>
      </div>
    </div>
  </div>

  <!-- FOOTER -->
  <div style="text-align:center;margin-top:36px;padding-top:20px;border-top:1px solid #27272a;color:#3f3f46;font-size:12px;">
    <div style="margin-bottom:10px;">
      Generated by <a href="https://www.devopsarg.com" style="color:#C9A227;text-decoration:none;font-weight:600;">DevOps ARG FinOps Intelligence Platform</a>
      · {generated_at}
    </div>
    <div style="display:flex;justify-content:center;gap:20px;flex-wrap:wrap;">
      <a href="https://www.devopsarg.com" target="_blank" style="color:#52525b;text-decoration:none;font-size:11px;">🌐 devopsarg.com</a>
      <a href="https://www.devopsarg.com/en/blog/" target="_blank" style="color:#52525b;text-decoration:none;font-size:11px;">📝 Blog &amp; case studies</a>
      <a href="https://www.devopsarg.com/en/blog/aws-cost-optimization-case-study/" target="_blank" style="color:#52525b;text-decoration:none;font-size:11px;">💰 AWS savings case study</a>
      <a href="https://www.devopsarg.com/en/blog/finops-dashboard-grafana-prometheus/" target="_blank" style="color:#52525b;text-decoration:none;font-size:11px;">📊 FinOps dashboard</a>
      <a href="https://www.devopsarg.com/en/blog/karpenter-spot-scale-to-zero/" target="_blank" style="color:#52525b;text-decoration:none;font-size:11px;">⚡ Karpenter + Spot</a>
      <a href="https://www.devopsarg.com/en/#contact" target="_blank" style="color:#C9A227;text-decoration:none;font-size:11px;font-weight:600;">✉️ Contact / hire us</a>
    </div>
  </div>

</div>
</body>
</html>"""
