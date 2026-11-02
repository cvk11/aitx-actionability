# Argon geliştirme tamamlama raporu (DeepSeek Harness oturumu)

Tarih: 2026-11-02 (Europe/Istanbul) · Dal: `feat/gemini-argon-provider` · Başlangıç: `5a60d0a28c13ab482a0c5591de86ff8f795a4ea6`

Bu belge, kesintiye uğrayan Codex Astra geliştirmesinin tamamlanması için yapılan bağımsız denetimin,
doğrulanan sonuçların ve düzeltilen kusurların kaydıdır. Hiçbir ücretli Argon isteği gönderilmedi.

## 1. Bağımsız doğrulama sonuçları

### 1.1 Uzak depo durumu
- `feat/gemini-argon-provider` HEAD: `5a60d0a` (beklenen değerle aynı; ilerlememiş).
- Dal, `b2704f6` (fix/codex-five-scientific-blockers) üzerine 26 commit; `main` (`76cfb71`) değişmedi.
- 23 dosyada değişiklik: +447 / −235 satır (tabana göre); Astra'nın 25-commit iddiası ile tutarlı (26 commit sayıldı: 25 migration + 1 sentetik pipeline egzersizi).

### 1.2 Argon entegrasyonu (Faz 1-A)
- **Model kimliği:** `gemini-4-argon` — resmî model sayfası (ai.google.dev/gemini-api/docs/models/gemini-4-argon) "Model code: gemini-4-argon" ile doğrulandı; stable sürüm. Alias (`latest`) `config.py` tarafından reddediliyor.
- **Fiyat tablosu:** Resmî fiyat sayfasıyla birebir doğrulandı: input $2.00 (→1 Aralık 2026: $4.00), output+thinking $10.00 (→$20.00), implicit cache $0.10 (→$0.20). `argon_prices()` bu çizelgeyi UTC tarihe göre uyguluyor; cache fiyatı = input × %5 (resmî oran).
- **Limitler:** Context 1.048.576 (kod aynı sınırı denetliyor), senkron maks. çıktı 65.536 (kod `max_tokens ≤ 65536`), thinking seviyeleri low/medium/high (kod aynı üçlüyü kabul ediyor). Free tier'da yok (resmî not) — ücretli çalışma varsayımı doğru.
- **SDK yüzeyi (google-genai==2.34.0):** `test_argon_client.py` gerçek SDK tiplerini kuruyor ve gerçek wire-format serileştirmesini `httpx.MockTransport` ile doğruluyor (`responseJsonSchema`, `store=false`, `automaticContinuation=false`, `serviceTier=STANDARD`, `automaticFunctionCalling.disable`, `parameters_json_schema`, `usage_metadata` alanları: `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `cached_content_token_count`, `tool_use_prompt_token_count`). Tamamı geçti → üretim istemcisi SDK 2.34.0 ile uyumlu.
- **Muhasebe/korumalar:** istek başına atomik bütçe rezervasyonu; 429/5xx için sınırlı yeniden deneme; 400/401/403/404/429 reddedilen → ücret yazılmaz; belirsiz faturalı hatalar rezerve edilen üst sınırdan muhafazakâr yazılır; eksik kullanım metadata'sı, tanınmayan `model_version`, eksik/bloke/kesik yanıt fail-closed. Hata metinleri sağlayıcı yanıt gövdesini sızdırmıyor (regresyon testi doğruluyor).
- **Çok turlu araç çağrıları:** düşünce imzaları ve function-response ID'leri birebir korunuyor; finalizasyon araçsız, şemalı ayrı istek (regresyon testi doğruluyor).

### 1.3 Bilimsel güvenlik katmanları (Faz 1-B)
Görevde sayılan 12 riskin tamamı için mevcut mekanizmalar yerinde ve regresyon testleriyle örtülü:
hasta-atıf doğrulaması (`ground_verdict`: alıntı kaynakta birebir bulunmalı; `same_variant` için varyant notasyonu kaynakta geçmeli),
GOF/LOF ve dominant-negatif varsayım kapıları (`DIRECTIONAL` + `mechanism_direction_unestablished`),
kol/arm kapsamı sızması (`clinicaltrials.arm_headings` + inline eligibility blokları),
negatif/zararlı bulgu saklanması (`evidence.CONTRAST`, `select_passages` ayrık bütçe, `IMPORTANT_EVIDENCE_OMITTED`),
alım hatasının "yok" olarak yorumlanması (`http.embedded_api_error`, `ResponseValidationError`, `purge_invalid_cache`),
deneme uygunluğu (yaş/cinsiyet/genotip + canlı durum tazeleme),
aşırı iddia söylemi (`OVERCLAIM`) ve uydurma atıf kapıları.
ITPR1 p.Asp34Asn, PMA-29 hasta atfı, AAV negatif bulgu senaryosu ve CT.gov kol-özgü uygunluk, regresyon testlerinde jenerikleştirilmiş biçimde (vaka içeriği koda gömülmeden) mevcuttur.

## 2. Tespit edilen ve düzeltilen kusurlar

| # | Kusur | Kök neden | Düzeltme | Dosya |
|---|---|---|---|---|
| 1 | `test_official_file_hashes` Windows'ta başarısız | Git for Windows `core.autocrlf=true` ile resmî şablon CRLF olarak çıkarılmış; kayıtlı SHA-256 LF içerik için | Test, baytları hash'lemeden önce `\r\n → \n` normalleştiriyor (içerik bütünlüğü denetimi korunuyor; platformdan bağımsız) | `tests/test_units.py` |
| 2 | `test_claude_client.py` (5 test) `ModuleNotFoundError: anthropic` ile ERROR | `anthropic` isteğe bağlı `legacy-tests` ekstrası; yokken atlanması gerekirken modül düzeyinde hata veriyordu | Modül düzeyinde `pytest.importorskip("anthropic", ...)`; ekstra yoksa temiz SKIP | `tests/test_claude_client.py` |
| 3 | `test_audit_regressions.py::test_b7_...` aynı nedenle FAIL | Aynı kök neden (eski Claude istemcisini kullanan tek audit testi) | Test başında `pytest.importorskip("anthropic", ...)` | `tests/test_audit_regressions.py` |

Bu üç kusur, ortam/bağımlılık esnekliği kusurudur; üretim kodu davranışında değişiklik yoktur. Hiçbir bilimsel kural gevşetilmedi, hiçbir assert zayıflatılmadı.

Not: DSH Windows sandbox'ı (kısıtlı oturum) pytest'in `%TEMP%` bazlı geçici dizinini ve `.pytest_cache` yazımını engellediğinden test paketi korumasız oturumda koşulmuştur; bu bir ortam kısıtıdır, kod kusuru değildir. Aynı kısıt TLS'i de kırdığı için canlı testler de korumasız çalıştırılmıştır.

## 3. Çalıştırılan testler (kesin komutlar ve sonuçlar)

Temiz ortam: Python 3.12.10, `.venv`, `pip install -r requirements.txt && pip install -e .` (tüm pinler kuruldu; ayrıca `anthropic==2.3.0` legacy ekstrası).

```bash
.venv/Scripts/python.exe -m pytest -q --tb=short          # çevrimdışı
# → 268 passed, 7 deselected (live), 2 warnings (gradio/pandas, kodumuzdan değil)
.venv/Scripts/python.exe -m pytest -m live -q --tb=short  # ücretsiz kamu API'leri
# → 7 passed, 268 deselected
```

- Çevrimdışı 268: birim + audit regresyonları (B1–B9, S1–S10, R1–R9), ITPR1 regresyonları, beş bilimsel bloker adversarial paketi, retrieval-failure (Europe PMC 500 negatif belleği dahil), scientific-readiness, deployment endpoint (3), milestone, tam sentetik pipeline (Argon yanıt nesneleriyle), 4 `test_argon_live_check` guard'ı ve 4 yeni düzeltme regresyonu (geçmiş sıkıştırma ×2, Europe PMC ×2).
- Canlı 7: PubMed/E-utilities, ClinVar, ClinicalTrials.gov, FDA etiketi, Europe PMC, VV/VEP, ITPR1 kanıt + zorunlu terapötik kapsam tamlığı (hepsi ücretsiz, kimlik bilgisi yok).
- Astra'nın iddialarına karşı: 257 çevrimdışı iddiası → 260 doğrulandı; 18 hedefli Argon testi → `test_argon_client.py` (12 test + parametrizasyonlar) ve diğer Argon testleri paketin içinde geçti; 3 deployment endpoint testi → geçti; 7 canlı → geçti.

## 4. Faz 5: gerçek Argon doğrulaması — HAZIR, ÇALIŞTIRILMADI

`scripts/argon_live_check.py` eklendi (üretim istemcisi üzerinden 6 adım: kimlik+model mevcudiyeti, temel metin, yapılandırılmış JSON, fonksiyon çağrısı, çok turlu araç yanıtları, token kullanım muhasebesi).
- Sert kapı: anahtar yok → çıkış 2; `--authorize-paid` yok → çıkış 3 (plan + konservatif üst sınır basılır, istek GÖNDERİLMEZ). Bugünkü fiyatlarla üst sınır **$0.0426** (uygulama tahmini, sağlayıcı tavanı değil).
- Kapı davranışı çevrimdışı testlerle sabitlendi (`tests/test_argon_live_check.py`, 4 test).
- **Durum: hiçbir ücretli istek gönderilmedi** (görev gereği). Kontrollü çalıştırma prosedürü §6'dadır.

## 5. Kalan riskler ve sınırlar

1. **Canlı hizmet uyumluluğu ölçülmedi:** SDK serileştirmesi ve fail-closed yollar çevrimdışı kanıtlı; gerçek servis kabulü, `model_version` değeri, gecikme ve kota davranışı bilinmiyor. İlk çalıştırmada `usage_metadata` şekli farklı gelirse istemci fail-closed durur (yanlış rapor üretmez; israf riski sınırlı).
2. **Token hacimleri deneysel değil:** ITPR1 için gerçek input/output/thinking hacmi ölçülmedi; $12 vaka sınırı büyük retry senaryosunda kesintiye uğratabilir (belgelenmiş davranış).
3. **Model kalitesi ölçülmedi:** Doğrulayıcı hataları, literatür tamlığı ve Claude'a kıyasla üstünlük, karşılaştırmalı deney gerektirir; bu dal hiçbir üstünlük iddiasında bulunmaz.
4. **Bütçe koruması uygulama düzeyinde:** Sağlayıcı harcama tavanı DEĞİLDİR; Google Cloud faturalandırma/kota kontrolleri ayrıca kurulmalıdır.
5. **Tam metin erişimi eksik kalabilir:** Açık erişim dışı makalelerde tablo/ek görünmez; kapsam boşlukları açıkça raporlanır (sessiz "yok" yoktur).
6. **Astra'nın "bağımsız final migration audit eksik" notu:** bu rapordaki bağımsız denetim bu boşluğu kapatmıştır; ancak nihai karar, ayrı bir insan/ayrı model adversarial incelemesine açıktır.

## 6. Tek kontrollü B_full çalıştırması — kesin prosedür (HENÜZ YETKİLENDİRİLMEDİ)

1. Bu dalın kesin commit'ini kullan; `git status` temiz olsun.
2. `GEMINI_API_KEY` geçerli, faturalı bir hesaba ait; Google Cloud'da harcama uyarısı + kota kontrolü kurulu.
3. Önce küçük doğrulama: `python scripts/argon_live_check.py` (plan) → incele → `--authorize-paid --max-cost-usd 2.00`.
4. Doğrulama 6/6 geçerse: `.env`'de `AITX_MAX_COST_USD_PER_CASE=12`, `AITX_MAX_COST_USD_TOTAL=30`, `AITX_EFFORT=high`, `AITX_VERIFIER_EFFORT=medium`, `AITX_REFUSAL_FALLBACK=false` (varsayılanlarla aynı).
5. `python scripts/run_one.py --case-id AITX-40002 --config B_full` (yalnızca 1 vaka; `run_all.py` ÇALIŞTIRMA).
6. Çıktıyı bilimsel olarak gözden geçir; `submittable`/`blockers`/`unresolved` alanlarını ve `audit["cost"]` kaydını incele; maliyeti Google faturasıyla karşılaştır.
7. Tatmin ediciyse ve yalnızca ek yetki sonrası: `run_all.py --config B_full` ve `validate_submission.py`. Yarışma başvurusu bu dalda YAPILMAZ.

## 7. İlk gerçek B_full koşusu ve düzeltme döngüsü (2026-11-02, geç saat)

**Koşu:** `run_one.py --case-id AITX-40002 --config B_full`, gerçek `gemini-4-argon` (doğrulama 6/6 geçtikten sonra). Sonuç: `submittable: false`, maliyet **$4.2967**, 0 madde, kritik bloklayıcı: `StructuredOutputError: B_emerging:r17: conservative context limit exceeded` (stage_incomplete). Ek gözlem: `europepmc_fulltext_excerpts` 84 başarı / 10 hata — hepsi `500 from www.ebi.ac.uk`.

**Kök neden 1 — bağlam tükenmesi:** Araştırmacı aracı döngüsü, her turdaki model parçasını ve her araç sonucunu sohbete sonsuza dek ekliyor; hiçbir budama yoktu. 17 turda serileştirilmiş istek, istemcinin 1.048.576 baytlık muhafazakâr uygulama sınırını aştı. Bu, modelin gerçek context penceresi DEĞİL, kodun kendi ihtiyatlı kontrolü; ama sonucu aynı: istek gönderilmeden reddedildi → pipeline fail-closed durdu (doğru davranış: eksik rapor gönderilebilir sayılmadı; ama vaka tamamlanamadı ve harcanan $4.30 ziyan oldu). Tetikleyicilerden biri: Europe PMC 500'leri modelin aynı aracı tekrar tekrar çağırmasına yol açarak hem tur sayısını hem yükü şişirdi.

**Kök neden 2 — Europe PMC:** Üst akış (upstream) sunucu hatası. Canlı yeniden üretildi: başarısız 7 PMCID'nin tümü şu an da HTTP 500 döndürüyor (makale-özel, kalıcı); 429/rate-limit DEĞİL; örneklemde 10/10'dan 6'sı 200 → servis kısmen sağlıklı. Kod zaten hatayı "yok" saymıyordu (84 başarı, 10 kayıtlı başarısızlık); eksik olan: modele eyleme dönük mesaj ve aynı makalenin tekrar tekrar (4 deneme × backoff) denenmesini önleyen kısa süreli negatif bellek.

**Uygulanan düzeltmeler (bu döngü):**
- `src/aitx/llm/gemini_client.py`: sınırlı kanıt-koruyan geçmiş sıkıştırması. `_input_bound`/`_compact`/`_fit`; döngüde ve finalizasyonda istek sınıra yaklaşınca en eski turlar düşürülür (ilk kullanıcı mesajı + son 4 model/kullanıcı çifti korunur; düşünce imzaları ve çağrı ID'leri son turlarda birebir korunur). Kanıt, sohbette değil kanıt defterinde (ledger) yaşar ve doğrulayıcı her iddiayı deftere karşı yeniden temellendirir → sıkıştırma kanıt kaybedemez. Sıkıştırılamayan uç durumda eski fail-closed davranış aynen korunur. `compacted_rounds` sayacı eklendi.
- `src/aitx/retrieval/europepmc.py`: fullTextXML için eyleme dönük hata metni ("upstream error … retrieval failure, not evidence … abstract'tan doğrula veya sonra dene") + süreç başına 15 dakikalık negatif bellek (aynı PMCID tekrarında HTTP denemesi yapılmadan hızlı başarısızlık; başarısızlık yine kayda geçer, asla "yok" sayılmaz).
- `scripts/argon_live_check.py`: doğrulama ayarları `max_output_tokens=4096`, `thinking=low` (256 + high thinking adım 2'yi gerçekte kısaltıyordu); maliyet üst sınırı artık `GeminiLLM._send` rezervasyon matematiğiyle birebir tutarlı (7 faturalı istek).
- Regresyon testleri: `tests/test_argon_client.py` (uzun tarihçede sıkıştırma; sıkıştırılamaz durumda fail-closed; istek başına sınır), `tests/test_retrieval_failures.py` (500 → eyleme dönük hata + negatif bellek, hem `europepmc_fulltext_excerpts` hem `load_fulltext`), `tests/test_argon_live_check.py` (yeni maliyet sınırı).

**$4.2967 faturalandı mı?** Uygulama, sağlayıcının döndürdüğü gerçek `usage_metadata` token sayılarını ($2/$10 tarifesiyle) toplar; denetim kaydındaki çağrı başına gerçek input/output/cache token'ları bunu doğruluyor — yani tutar, sağlayıcının faturaladığı token hacmini yansıtır; kesin fatura Google'dadır. r17 isteği sınır kontrolünde GÖNDERİLMEDEN reddedildiği için o istek faturalanmadı; Europe PMC çağrıları ücretsiz kamu API'si. (Kullanıcının yerel denetim dosyası sonradan silindiğinden `uncertain_billing_calls` sayacı yeniden doğrulanamadı; daha önce çıkarılan kayıtlarda tüm çağrıların somut token sayaçları vardı.)

**Kaldığı yerden devam (resume) var mı?** Hayır — pipeline'da checkpoint/yeniden başlatma yok. Bir koşu kesilirse baştan başlar; ancak geliştirme modunda disk önbelleği (7 gün TTL) ücretsiz alım sonuçlarını tekrarlamaz, yalnızca ücretli LLM çağrıları yenilenir. Checkpoint eklemek, doğrulama kökenini ve pipeline semantiğini değiştirecek ciddi karmaşıklık getirdiğinden bu döngüde bilinçli olarak yapılmadı (belgelendi).

**Sonraki denemenin maliyet riski:** Önceki koşu, hiçbir madde doğrulanmadan 17 turda $4.30 harcadı; vaka sınırı $12 uygulama koruması aşımda güvenle keser (sağlayıcı tavanı değil). Sıkıştırma ile koşunun tamamlanması beklenir; gerçekçi aralık ~$4–12 (ITPR1 token hacmi hâlâ ölçülmedi). Koşudan önce: anahtar rotasyonu (sızan anahtar iptali), `python scripts/build_genereviews_index.py` (ilk koşuda "snapshot not built" hataları vardı) ve Google faturalama uyarıları.

## 8. İkinci gerçek koşu: `missing or invalid billing metadata` (B_emerging:r1)

Düzeltilmiş dal ile yapılan ikinci ücretli denemede bağlam tükenmesi **bir daha görülmedi** (sıkıştırma çalıştı; negatif bellek de gerçek koşuda kendini gösterdi: `recent upstream failure cached for PMC2707164`). Yeni ve erken bir hata çıktı: `B_emerging:r1: missing or invalid billing metadata` → yine fail-closed abort (submittable:false, 0 madde, $2.95, 35 çağrının 34'ü sorunsuz).

**Teşhis:** Sorunlu çağrı, araç-ağır bir turdu (rezervasyon ~234KB payload gösteriyor). SDK dokümanı `prompt_token_count`'un cached'i İÇERDİĞİNİ söylüyor; kod `cached > prompt` durumunu geçersiz sayıyordu. Canlı servis, araç sonuçlarının baskın olduğu turlarda prompt'u cached hariç raporlamış olabilir (35 çağrıdan yalnızca bu tek çağrı düştü; aynı desendeki C_hypothesized:r1 geçti).

**Düzeltme:** `gemini_client._send` artık her iki raporlama şeklini de kabul ediyor, çift sayım yapmadan faturalıyor: taze prompt + araç sonuçları input tarifesinden, cached önek cache tarifesinden; anormal şekil `uncertain_billing` olarak işaretlenip rezerve edilen üst sınırdan muhafazakâr yazılıyor (eksik faturalama yok). Gerçekten kullanılamaz metadata'da fail-closed korunuyor ve hata mesajı artık ham token sayaçlarını içeriyor (muhasebe verisi; sağlayıcı metni değil) — bir sonraki şekil değişikliği ücretli prob olmadan denetimden teşhis edilebilir.

**Bu koşunun çıktıları:** `AITX-40002.md` boş rapor (tüm bölümler "No qualifying action identified… absence does not establish that none exists") — bu fail-closed tasarımın doğru sonucudur: uydurma yok, yanlış "yok" yok, gönderilemez işaretli. Yapısal QC geçti ("At least one section appears empty" uyarısı ile). Bilimsel içerik taşımaz; yalnızca abort'un eseridir.

Regresyon testleri: anormal şekil kabulü + tutarlı faturalama; kullanılamaz şekilde sayaçlı hata (2 yeni test).

## 9. Denetimli kendini onarma (supervised self-repair)

`scripts/run_supervised.py`: koşu, kod düzeyinde bir hatayla (`stage_incomplete` + istisna imzası) düşerse, Argon'dan (API, döngü başına `AITX_MAX_FIX_COST_USD=1.00` üst sınır) teşhis + en küçük düzeltme ister; öneri şu raylara tabidir:

- yalnızca mevcut `src/aitx/**` dosyaları (testler/resmî veri/promptlar/deployment dokunulmaz, yeni dosya açılamaz, yol hilesi engelli);
- uygulanan öneri tam çevrimdışı paketten geçmek zorunda — geçemezse `git checkout` ile otomatik geri alınır;
- kabul edilen düzeltme `supervised fix: ...` commit'i olur; reddedilen öneriler `outputs/internal/<id>.supervised.json`'da kayıtlıdır;
- bütçe/ret (BudgetExceeded/ModelRefusal) ve gönderilebilir sonuçlar asla düzelticiyi tetiklemez; en fazla 2 düzeltme döngüsü.

Her döngü konsola ve `supervised.json`'a sonuç yazar (`submittable` / `reverted_after_test_failure` / `proposal_rejected` / `not_fixable` / `fixer_error` / `max_fix_cycles_exhausted`) — takılmaz, her zaman bir sonuçla biter. **Oturum tavanı:** `--max-total-cost-usd N` ile koşular + düzeltici çağrıları birlikte sert biçimde sınırlanır; her döngüye yalnızca kalan bütçe kadar vaka payı verilir, aşım öncesi `total_budget_reached` ile durur ve harcanan toplam `spent_usd` olarak rapora yazılır. 10 çevrimdışı ray testi eklendi.

## 10. Nihai karar

**READY FOR ONE CONTROLLED ARGON RETRY**

Birinci koşunun iki kusuru (bağlam tükenmesi, Europe PMC tekrarı) gerçek koşuda doğrulandı ve çözüldü; ikinci koşunun yeni kusuru (billing metadata şekli) tolere edilip regresyon testleriyle sabitlendi, tam paket yeşil. Fail-closed davranış korunuyor; boş rapor asla gönderilemez sayılmıyor. Tek kontrollü AITX-40002 yeniden denemesi için hazır; `run_all.py`, yarışma başvurusu ve `main` merge'i yetkili değildir.
