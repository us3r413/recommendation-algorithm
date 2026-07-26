Detailed design third draft

資料數
1,000,000 -> a lot of filtering -> 10


use duckdb to speed up querying

瀏覽次數.csv
-根據職缺瀏覽.csv 所構建而成的 因為職缺本身沒有記錄瀏覽次數的表格 而職缺瀏覽就只是職缺得訪問log
-可以考慮加入主動應徵log
-記錄方式的方案
採納A) 主動應徵*weight+瀏覽次數*weight 以 score的方式儲存
加入time decay

func querytoRequirement (str) -> (str(tags)) 
-利用LLM將query內容的自然語言轉換成json格式
-spellcheck
-LLM分析query語意(例如pt = 兼職)並整理成json格式

func grabFromDatabase (str (tags)) -> (file log)
-從tag來尋找database的資料
-對部分表格進行語意延伸 (例如 軟體工程師也可以叫軟體設計師、兼職 = 打工 = 工讀)
-從職缺表尋找職缺

func ranking (file log)  -> (top 10 ranked)
-如果使用者未登錄(talentNo = 0)那就直接透過瀏覽次數.csv來進行排序
-但是如果使用者有登錄過 那就要根據過去的應徵紀錄來排序
-方案(不管哪個都可能需要用到graph rag like neo4j)
採納C)先建立一個所有user的behavior pattern log來快速篩選
-先暫時不要用graph 因為不太好在比賽情況下implement 
-從user behavior pattern 來未有相似的條件打分數 例如地區 薪水大於多少