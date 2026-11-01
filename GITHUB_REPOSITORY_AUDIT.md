# GitHub Deposu Teknik Denetimi — `cevikbilgin-rad/llm-radiology-uncertainty`

**Yöntem:** Depo 2026-11-01'de `git clone` ile yerel olarak klonlandı (orijinal depo **değiştirilmedi**; hiçbir push/commit yapılmadı). Güncel ağaçtaki her dosya okundu; ayrıca Git geçmişinde 2026-08-04 tarihli `89ec8f6` commit'i ile silinen 10 Python betiği `git show 89ec8f6^:<dosya>` ile okundu. Depo geçmişi: 26 commit, 2026-05-19 → 2026-08-23.

## 1. Genel mimari

Depo, *ordinal dilsel belirsizlik derecelendirmesi* (göğüs grafisi raporları, 0–3) çalışmasının **hakem odaklı, türetilmiş veri yeniden üretim paketidir** (manuscript DIR-2026-192). Ham rapor metni, model gerekçeleri, ham API yanıtları ve anahtarlar bilinçli olarak dışarıda bırakılmış. **Güncel ağaçta canlı LLM çıkarımı yapan hiçbir betik yoktur**; çıkarım kodu yalnızca Git geçmişinde mevcuttur.

```
README.md, LICENSE (MIT), requirements.txt, SHA256SUMS.txt, .gitignore
prompts/locked_prompt.txt
scripts/  reproduce_validation_statistics.py | reproduce_discovery_summary.py | reproduce_robustness.py
data/     validation/ discovery/ robustness/ audits/   (yalnızca ID + türetilmiş skorlar)
metadata/ claude_inference_provenance.json, gemini_3827_batch_metadata.json, gemini_3827_batch_summary.txt
outputs/  manuskript tabloları (CSV)
docs/     DATA_DICTIONARY.md, REPOSITORY_MANIFEST.md
```

## 2. Dosya-dosya analiz

### 2.1 Güncel ağaç

| Dosya | İçerik / işlev | Canlı çıkarım? | AI-Tx için değer |
|---|---|---|---|
| `README.md` | Çalışma tasarımı, tarihli model kimlikleri (`claude-opus-4-5-20251101`, `gemini-2.5-pro-preview-05-06`, `gemini-2.5-pro`), sonuç tabloları, kısıtlar ve açık kontaminasyon uyarısı | – | **Metodoloji örneği:** tarihli model kimliği + yapılandırma tablosu, sonuçların sınır olarak yorumlanması |
| `requirements.txt` | numpy/pandas/sklearn/statsmodels (alt sınırlı, sabitlenmemiş) | – | Ders: AI-Tx'te sürümler **tam sabitlendi** (`==`) |
| `prompts/locked_prompt.txt` | Kilitli prompt; birincil kategorik çıktı + ikincil tanımlayıcı skor ayrımı; "Respond ONLY with valid JSON in this exact format" | – | **Yeniden kullanıldı (ilke):** dosyada sürümlenen, SHA-256'sı provenance'a yazılan promptlar (`aitx/prompts.py`) |
| `scripts/reproduce_validation_statistics.py` | `qwk()`, `bootstrap_qwk()` (seed 42, 1000 resample), `paired_delta()`, `fleiss_kappa()`, `gwet_ac()` (identity/linear/quadratic), `bootstrap_multi()`, `method_metrics()` (konfüzyon, sınıf-bazlı recall, ±1 hata sayıları) | Hayır | Ordinal κ AI-Tx çıktısına doğrudan uymaz; **bootstrap + eşleştirilmiş fark** fikri tekrarlı-çıkarım karşılaştırmasına aktarılabilir |
| `scripts/reproduce_discovery_summary.py` | Wilson CI'lı dağılımlar, modeller arası QWK, token muhasebesi (ortalama/SD/medyan/IQR/toplam) | Hayır | **Token/maliyet muhasebesi** fikri → `CostTracker` (aşama bazlı) |
| `scripts/reproduce_robustness.py` | 3 prompt varyantı arası κ; 3 tekrarlı çalışmada "üçü de aynı" oranı + ikili κ | Hayır | **Doğrudan uyarlandı:** `aitx/robustness.py` (bölüm bazlı eylem kümesi Jaccard, tier yerleşim tutarlılığı, bespoke verdict tutarlılığı, "tüm çalıştırmalarda aynı" oranı) |
| `metadata/*.json` | Model kimliği, sıcaklık, batch modu, prompt SHA-256 ve **iki hash'in eşdeğerliği belgelenmedi** notu; eksik metadata dürüstçe `null` | – | **Yeniden kullanıldı (ilke):** her çalıştırmada `audit.json`: ayarlar (sırsız), prompt hash'leri, yazılım sürümleri, sunulan model, token/maliyet, GeneReviews arşiv SHA-256'sı |
| `data/audits/claude_locked_vs_later_validation_audit.csv` | Kilitli kaynak ile sonraki tam korpus çalıştırması arasındaki 6 farkın denetimi | – | **İlke:** gönderilen raporlar ile uç nokta çıktısı arasındaki farklar belgelenmeli (FAQ "closely resemble") |
| `data/audits/lexicon_*` | Alt-dize eşleşmesi kaynaklı yanlış pozitifler ("may" ⊂ "Maybe") ve en uzun eşleşme denetimi | – | Ders: basit dize eşleştirme tehlikeli → atıf anahtarı ayrıştırma regex'le sınırlandı ve test edildi |
| `outputs/*.csv` | Manuskript tabloları | – | İlgisiz |
| `docs/DATA_DICTIONARY.md`, `REPOSITORY_MANIFEST.md` | Veri sözlüğü, veri minimizasyonu ilkeleri | – | **İlke:** "no raw text / no raw responses / no keys" → dağıtım modunda değerlendirme verisi saklanmaz |
| `SHA256SUMS.txt` | Tüm dosyaların sağlama toplamları | – | **Yeniden kullanıldı:** `data/official/SOURCES.md` + `tests/test_units.py::test_official_file_hashes` |

### 2.2 Git geçmişindeki (silinmiş) betikler — `89ec8f6^`

| Betik | İşlev | Canlı çıkarım? | Değerlendirme |
|---|---|---|---|
| `01_data_preparation.py` | OpenI indirme (`requests`), XML ayrıştırma, sözlük tabanlı hedge sayımı | Hayır (LLM yok) | İlgisiz |
| `02_sampling.py` | 4 katmanlı tabakalı örnekleme, seed 42 | Hayır | İlgisiz |
| `03_claude_api.py` | **Evet**: `anthropic.Anthropic().messages.create(model="claude-opus-4-5-20251101", temperature=0, max_tokens=512)`; `parse_json_robust()` (``` çit temizleme + `json.loads`); 5 deneme, doğrusal geri çekilme; her 10 raporda checkpoint; yeniden başlatmada checkpoint'ten devam | **Evet** | Checkpoint/devam ve sağlam JSON ayrıştırma **uyarlandı** (`_json_from_text`). **Kusur:** son denemede de hata olursa `{"uncertainty_level": "none", "claude_score": 0}` döndürüyor → başarısız çağrılar sessizce "belirsizlik yok" olarak kodlanıyor (sistematik yanlılık). AI-Tx'te **asla sessiz varsayılan yok**: hatalar araç sonucunda `is_error`, denetim izinde `unresolved` olarak görünür; yapılandırılmış çıktı Pydantic ile doğrulanır |
| `04_gemini_api.py` | **Evet**: `google.genai` `generate_content`, `response_mime_type="application/json"`, temp 0 | **Evet** | Aynı sessiz-varsayılan kusuru. Gemini istemcisi `GenerateContentConfig(response_schema=PydanticModel)` ile yeniden yazıldı |
| `05_consensus.py` | Çoğunluk oyu + kıdemli okuyucu bağ çözümü; nötr sütun eşlemesi | Hayır | **Fikir:** "uyuşmazlıkta önceden belirlenmiş hakem" → Gemini–Claude anlaşmazlığında **kanıta dayalı hakemlik** (ledger'daki kaynakla karar; Gemini görüşü tek başına geçersiz) |
| `06_statistics.py` | κ + CI, Fleiss, Gwet AC1 (irrCAC), Landis-Koch etiketleri (CI alt sınırıyla — muhafazakâr), `run_llm_lexicon_probe` (**LLM çağırmıyor**, hazır skorları karşılaştırıyor) | Hayır | **İlke:** muhafazakâr etiketleme → kanıt düzeyi etiketleri raporda açıkça yazılıyor |
| `07_hallucination_check.py` | Modelin döndürdüğü `key_phrases`'in kaynak metinde birebir geçip geçmediği (verbatim / partial / fabricated); sınırlaması açıkça yazılmış: *"Does NOT prove absence of semantic/contextual misattribution"* | Hayır | **En değerli fikir — genişletildi:** AI-Tx'te (a) deterministik varlık kontrolü (PMID esummary, NCT canlı kayıt) + (b) **anlamsal** iddia–kaynak uyumu için ayrı bir doğrulayıcı LLM. Orijinal çalışmanın açıkça kabul ettiği sınırlama (anlamsal hatayı yakalamaması) burada hedeflenen boşluk |
| `08_clinical_domain_analysis.py` | Keşif kohortunda tanımlayıcı alan analizi (p-değeri yok) | Hayır | İlgisiz |
| `09_stochasticity_test.py` | Önceden hesaplanmış veride analiz; `--live` ile **gerçek** 3 tekrarlı Claude çağrısı; hata → `-1` ve "tutarsız" sayılır | `--live` modunda **evet** | **Uyarlandı:** `scripts/robustness.py --runs N` (ücretli, onay gerekir); hatalar gizlenmez |
| `10_prompt_sensitivity.py` | 3 prompt varyantı (orijinal/kısa/CoT); `--live` ile gerçek çağrılar | `--live` modunda **evet** | Fikir: prompt duyarlılığı → AI-Tx'te prompt hash'leri sabit; varyant analizi gelecekteki iş |

## 3. Yeniden kullanım kararları

**Doğrudan yeniden kullanılabilir fonksiyon:** Yok denecek kadar az — kod ordinal sınıflandırmaya özgü. `parse_json_robust` mantığı (çit temizleme) `_json_from_text` içinde, `{…}` aralığını çıkarma yedeğiyle birlikte yeniden yazıldı. İstatistik fonksiyonları (`qwk`, `gwet_ac`) AI-Tx çıktısına uygulanamaz (ordinal etiket yok).

**Uyarlanan metodolojiler:**
1. Kilitli, dosyada sürümlenen prompt + SHA-256 provenance.
2. Tarihli/sabit model kimliği ve tam yapılandırma kaydı (sıcaklık yerine artık `effort`, çünkü güncel API örnekleme parametrelerini reddediyor).
3. Tekrarlı-çıkarım sağlamlık analizi (3 çalıştırma, "tümünde aynı" oranı) → küme-uyum metrikleriyle.
4. Kaynak metinde dayanak kontrolü (verbatim) → deterministik varlık + anlamsal doğrulama.
5. Uyuşmazlıkta önceden tanımlı hakem → kanıta dayalı hakemlik.
6. Veri minimizasyonu → dağıtımda kayıt/saklama yok.
7. Dürüst eksik-metadata bildirimi (`null` + not) → `unresolved` listesi ve "searched but not found" ifadeleri.

**Yeniden yazılması gerekenler:** Tüm çıkarım katmanı (çok adımlı ajan döngüsü, araç kullanımı, yapılandırılmış çıktı, geri dönüş/fallback, bütçe), hata işleme (sessiz varsayılan yerine açık hata).

**İlgisiz:** OpenI indirme/sözlük, tabakalı örnekleme, okuyucu-arası uyum, alan analizi, manuskript tabloları.

## 4. Rekabet avantajı sağlayan fikirler

- Çalışmada aynı agregat QWK'ya rağmen modellerin nadir yüksek-belirsizlik sınıfında farklı davrandığı bulunmuş (Score-3 recall 5/8 vs 1/8). AI-Tx'e aktarımı: **toplam rapor uzunluğu/kapsamı değil, kritik nadir öğeler** (yanlış tier, yanlış deneme durumu, varyanta uymayan mekanizma) belirleyici → doğrulama bu öğelere odaklandı.
- Kontaminasyon farkındalığı (OpenI muhtemelen ön-eğitimde): AI-Tx'te modelin "hafızasından" PMID/NCT üretmesi yasak; yalnızca bu çalıştırmada araçla getirilmiş ledger anahtarları atıf olabilir — yapısal olarak zorlanıyor.

## 5. Bileşen uzlaştırma tablosu (Part 6)

| Bileşen | Mevcut GitHub projesi | AI-Tx gereksinimi | Yeniden kullanım stratejisi |
|---|---|---|---|
| Kilitli prompt | `prompts/locked_prompt.txt` | Sabit, sürümlenen, hash'li promptlar | `prompts/*.md` + `aitx/prompts.py` (SHA-256 manifest her audit'te) |
| Model sabitleme | README tablosu + `metadata/claude_inference_provenance.json` | Tam sürüm sabitleme (FAQ) | `config.py` "latest" içeren kimlikleri reddeder; served model her çağrıda kaydedilir |
| JSON çıktı | "Respond ONLY with valid JSON" + `parse_json_robust` (`03_claude_api.py`) | Yapılandırılmış ara çıktılar | `output_config.format` JSON şeması + Pydantic doğrulama + tek tekrar; `_json_from_text` |
| Hata işleme | Son hata → skor 0 (sessiz) (`03`, `04`) | Hatalar asla gizlenmez | Araç hataları `is_error`, `unresolved` listesi, bütçe aşımı açıkça raporlanır |
| Claude vs Gemini | Bağımsız sınıflandırıcılar, κ karşılaştırması (`06`) | Gemini isteğe bağlı bağımsız gözden geçirici | `llm/gemini_client.py` + Claude hakemliği (`agents/reviewer.py`) |
| Konsensüs | Çoğunluk + kıdemli hakem (`05_consensus.py`) | Model anlaşmazlığı çözümü | Kanıta dayalı hakem; Gemini görüşü otomatik uygulanmaz |
| Halüsinasyon kontrolü | Verbatim ifade kontrolü (`07_hallucination_check.py`) | Atıf varlığı + iddia–kaynak uyumu | `agents/verifier.py` (deterministik + LLM), `rules.py` |
| Tekrarlı çıkarım | 3 çalıştırma, "üçü de aynı" (`09`, `reproduce_robustness.py`) | Kazanan doğrulamasında tutarlılık | `aitx/robustness.py`, `scripts/robustness.py` |
| Token muhasebesi | `reproduce_discovery_summary.py` | Maliyet/token takibi | `llm/base.py::CostTracker` (aşama, model, cache read/write) |
| Checksums | `SHA256SUMS.txt` | Resmî dosyaların bütünlüğü | `data/official/SOURCES.md` + test |
| Veri minimizasyonu | Ham metin yok (`REPOSITORY_MANIFEST.md`) | No-logging kuralı | Dağıtım modunda disk önbelleği/artefakt/günlük kapalı |

## 6. Göç stratejisi

1. Orijinal depo dokunulmadan referans olarak kalır (`/home/user/_ref/` altında salt-okur klon; yeni projeye kopyalanmadı).
2. Yeni proje (`aitx-actionability`) yalnızca ilkeleri ve yeniden yazılmış küçük yardımcıları içerir; radyolojiye özgü rubrik/terminoloji yok.
3. Gelecekte makale için: tekrarlı-çıkarım ve konfigürasyon karşılaştırması (yalnız Claude / +doğrulama / +Gemini) radyoloji çalışmasındaki raporlama disiplinine (önceden belirlenmiş analiz, tarihli model, sınırların açık yazımı) uygun biçimde raporlanabilir.
