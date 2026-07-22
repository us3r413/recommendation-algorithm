# Project Structure

```
recommendation-algorithm/
├── dataset/                        # Raw input data (CSV files, read-only reference)
│   ├── README.md                   # Full schema and relationship docs for all tables
│   ├── 職缺.csv                    # Job listings master table (primary key: 職缺編號)
│   ├── 職缺瀏覽_20260601_20260607.csv  # Job view behavior log
│   ├── 主動應徵_0601-0607.csv      # Job application behavior log
│   ├── userSearchLog_20260601_20260607.csv  # User search queries and results
│   ├── 城市對照表.csv              # City/region code lookup table
│   └── 職務對照表.csv             # Job category code lookup table
├── draft.md                        # High-level approach notes
├── modules.md                      # Detailed module design (functions, data structures)
├── README.md                       # Project root readme
└── requirements.txt                # Python dependencies (currently empty)
```

## Dataset Key Relationships

```
職缺.職缺編號  ──  主動應徵.empNo
職缺.職缺編號  ──  職缺瀏覽.employeeNo
職缺.職缺編號  ──  userSearchLog.empStr  (comma-separated)
職缺.廠商編號  ──  職缺瀏覽.organNo
userSearchLog.talentNo  ──  職缺瀏覽.talentNo  ──  主動應徵.talentNo
城市對照表.CodeNo  ──  userSearchLog.c0  (comma-separated)
職務對照表.CodeNo  ──  userSearchLog.d0  (comma-separated)
```

## Conventions

- `talentNo = 0` means anonymous/unauthenticated user — do not treat multiple rows with `talentNo = 0` as the same person.
- Multi-value fields (`empStr`, `c0`, `d0`) are comma-separated strings and must be split before use.
- City and job-category codes in search logs are numeric codes; job listings (`職缺.csv`) already contain Chinese name strings — cross-reference by name when joining.
- New derived tables (e.g. a computed popularity score table) should be documented in `modules.md` before implementation.
- Design decisions and algorithm options should be captured in `draft.md` (high-level) or `modules.md` (detailed).
