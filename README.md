# 西洋雕刻演進大全集 · Sculpture Atlas

與《西洋藝術流派大全集》同架構的互動式雕塑史：時間軸、40 個時期／流派與關係圖、雕塑家頁（生平／特色／軼事）、作品牆、依流派／時間／雕塑家／收藏地／主題／材質六種索引、看圖小測驗。

線上版：`https://briantsaivc-web.github.io/sculpture-atlas/`

## 檔案結構
```
index.html          單頁應用，啟動時 fetch data/*.json
data/movements.json 流派（id, zh, en, start, end, era, color, desc, traits, from[], against[], artists[]）
data/artists.json   雕塑家 { "英文名": {zh, birth, death, nat} }
data/bios.json      雕塑家介紹 { "英文名": {bio, style[], anecdote, keyworks[], aka[], confidence} }
data/works.json     作品（欄位同繪畫版，另加 mat 主要材質；wd/wd_flag 為 Wikidata 比對結果）
data/museums.json   收藏地 { id: {en, zh, city, country, count} }
data/themes.json    主題清單；data/materials.json 材質清單
img/800/<id>.webp   圖片（長邊 800px）；大圖視窗會自動載入 Commons 線上高解析度版
tools/validate.py   資料一致性檢查
```

## 資料品質
- 流派、雕塑家、作品、說明由 AI（Sonnet 子代理）依知識整理，未逐件人工查證；圖檔以 Commons 站內搜尋自動配對，再以 Wikidata 比對年代／館藏／作者，不符者標 `wd_flag`。
- 雕塑圖片為他人攝影作品，Commons 上皆為自由授權；仍受著作權保護的現當代雕塑（作者逝世未滿 70 年）不嵌圖，只給連結。
