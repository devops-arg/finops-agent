# Screenshots

Drop PNGs here with these filenames — the root `README.md` references them.

| Filename | What it should show | Recommended size |
|----------|---------------------|------------------|
| `logo.png` | DevOps ARG gorilla badge (transparent background) | 280×280 |
| `chat.png` | Chat tab with a conversation in progress and the reasoning trace panel visible on the right | 1800×1000 |
| `reasoning-trace.png` | Close-up of just the trace panel, showing `tool_call` / `tool_result` / `answer` events colored | 840×900 |
| `sidebar-questions.png` | The 27-preset-questions sidebar, fully expanded, showing a hovered tooltip | 640×900 |
| `dashboard-report.png` | Weekly cost report dashboard, bars / pie for service breakdown, 4-week trendline | 1800×1000 |
| `dashboard-infra.png` | Infrastructure view, EC2/RDS/EKS cards, with the `region=all` selector visible | 1800×1000 |
| `dashboard-optimize.png` | Optimization tab, Cost Optimization Hub recommendations listed with estimated monthly $ savings | 1800×1000 |
| `readonly-setup.png` | Terminal output of `./create-read-only.sh` with the 403 `s3 mb` verification step highlighted | 1200×600 |

**Tips:**
- Use a dark-mode OS theme so they match the product's cinematic aesthetic.
- Mock mode (`USE_MOCK_DATA=true`) produces realistic, non-stale numbers — perfect for screenshots without leaking real account data.
- Redact any AWS account IDs, ARNs, IAM UserIds, or email addresses before pushing.
- PNG, not JPG — sharper UI rendering.
- Optimize with `sharp` or `squoosh` before committing; aim for <300 KB per screenshot.
