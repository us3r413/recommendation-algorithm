import pandas as pd

city_df = pd.read_csv('dataset/城市對照表.csv')
job_cities = set(pd.read_csv('dataset/職缺.csv', usecols=['工作城市'])['工作城市'].dropna().unique())
print('Job cities:', sorted(job_cities))

# Try CodeNameA column for all rows
all_nameA = set(city_df['CodeNameA'].dropna().astype(str))
matches_A = job_cities & all_nameA
print(f'\nMatches using CodeNameA (all {len(all_nameA)} rows): {len(matches_A)}')
print(sorted(matches_A))

# Try just CodeType==2
type2 = city_df[city_df['CodeType'] == 2]
type2_names = set(type2['CodeNameA'].dropna().astype(str))
matches_type2 = job_cities & type2_names
print(f'\nMatches using CodeNameA CodeType==2 ({len(type2_names)} rows): {len(matches_type2)}')
print(sorted(matches_type2))

# What job cities are NOT matched?
unmatched = job_cities - all_nameA
print(f'\nJob cities NOT in CodeNameA: {sorted(unmatched)}')
