# Bilimsel kontroller (deterministik katman) — 2026-11-01

**Kapsam uyarısı:** Bu kontroller yalnızca LLM içermeyen deterministik katmanı (retrieval, HGVS/transkript modeli, ekson/NMD hesapları, kaynak durum sınıflandırması) doğrular. **Gerçek bir Claude çıkarımı henüz çalıştırılmadı**; dolayısıyla üretilecek raporların klinik/bilimsel geçerliliği bu belgeyle **kanıtlanmış değildir**. Yazılım testlerinin geçmesi bilimsel geçerlilik anlamına gelmez.

Yeniden üretim: `python scripts/scientific_checks.py` (ücretsiz; çıktı `outputs/internal/scientific_checks.json`). Pozitif kontrol olarak organizatörlerin örnek formatındaki CFTR p.Phe508del kullanıldı.

## 1. Test türlerinin ayrımı

| Tür | Ne doğrular | Durum |
|---|---|---|
| Çevrimdışı birim/entegrasyon testleri (mock LLM + sahte HTTP, kurgusal GENEX geni) | Kod yolları, kurallar, render, QC, uç nokta sözleşmesi | 53 test geçti |
| Canlı ücretsiz API testleri (`pytest -m live`, CFTR kontrolü) | Gerçek API'lerin yanıt biçimi ve ayrıştırma | 4 test geçti |
| Deterministik bilimsel kontroller (bu belge) | 4 resmi vaka + CFTR üzerinde gerçek veriyle varyant/kaynak katmanı | Aşağıda |
| **Gerçek LLM klinik çıkarımı** | Mekanizma yorumu, öneriler, iddia doğrulama kalitesi | **Çalıştırılmadı** (ücretli; onay bekliyor) |

## 2. HGVS ve transkript tutarlılığı / VEP–ekson hesapları

| Vaka | Varyant | VariantValidator | p. notasyonu uyumlu | Hesaplanan ekson | VEP ekson | Sonuç sınıfı (hesap / VEP) |
|---|---|---|---|---|---|---|
| AITX-40001 | NM_016188.5:c.1099dup p.(Gln367ProfsTer48) | geçerli, uyarı yok | evet | 12/14 | 12/14 | frameshift / frameshift_variant |
| AITX-40002 | NM_001378452.1:c.100G>A p.Asp34Asn | geçerli | evet | 4/62 | 4/62 | missense / missense_variant |
| AITX-40003 | NM_006415.4:c.116T>A p.Leu39His | geçerli | evet | 2/15 | 2/15 | missense / missense_variant |
| AITX-40004 | NM_005639.3:c.1103T>C p.Ile368Thr | geçerli | evet | 11/11 (son ekson) | 11/11 | missense / missense_variant |
| Kontrol | NM_000492.4:c.1521_1523del p.Phe508del | geçerli | evet | 11/27 | 11/27 | inframe_del / inframe_deletion |

Ekson hesabı NCBI RefSeq özellik tablosundaki ekson sınırlarından (transkript koordinatları) yapılır; VEP ile uyuşmazlık otomatik olarak `exon_consistent_with_vep=false` + uyarı üretir (testte doğrulandı).

## 3. NMD tahmini ve sınırları

- **AITX-40001:** PTC kodon ~414 → c.~1239 (referans koordinat; 1 nt dup kayması düzeltildi); son ekson c.1201'de başlıyor (son ekson-ekson bağlantısı c.1200) → **kural tabanlı tahmin: NMD'den kaçış (PTC son eksonda)**; C-terminali değişmiş kısaltılmış protein oluşabilir. Elle doğrulandı (ekson 12 = c.1018–1113, ekson 13 = c.1114–1200, ekson 14 = c.1201–1430).
- Diğer üç vaka missense: NMD hesaplanmaz; mekanizma (LoF / DN / GoF) kanıttan belirlenmeli.
- **Sınırlamalar:** (1) yalnızca kurallar (son ekson; son bağlantının ~50–55 nt yukarısı; başlangıca yakın PTC uyarısı) — deneysel NMD verisi değildir; (2) yalnızca verilen transkript modellenir (doku-özgü izoformlar, alternatif ekleme dikkate alınmaz); (3) uzun ekson/EJC-bağımsız NMD, yeniden başlatma ve okuma-geçişi modellenmez; (4) PTC konumu protein HGVS'ten (fsTerN) türetilir; protein notasyonu eksikse hesaplanmaz; (5) ENST girişlerinde transkript modeli kurulmaz. Rapor metninde bu, "rule-based prediction" olarak etiketlenir.

## 4. Varyanta özgü mekanizma sinyalleri (deterministik katmandan; yorum LLM aşamasında)

UniProt'un varyant kalıntısıyla örtüşen özellikleri artık doğru getiriliyor (alan adı hatası düzeltildi):
- **SPTLC1 L39:** UniProt kalıntı 39'da "Natural variant: in ALS27; increased production of sphinganine and ceramides…" notu taşıyor. Aynı gende HSAN1A (ClinGen Definitive) ile ALS27 farklı mekanizmalar/tedavi yönleri içerebilir → mekanizmanın gen düzeyinden değil varyanttan çıkarılması gerektiğinin canlı örneği. Sistem bunu kodlanmış cevapla değil, kanıt sinyali olarak yorumlayıcıya veriyor; doğru yorum **gerçek çıkarımda doğrulanmalı**.
- **SYT1 I368T:** UniProt "in BAGOS" doğal varyant kaydı; C2B alanı (273–406).
- **ACTL6B:** UniProt'ta DEE76 ile ilişkili kesik varyant kayıtları.
- **ITPR1 D34N:** yalnızca sitoplazmik bölge örtüşmesi; varyanta özgü fonksiyonel veri deterministik katmanda yok → mekanizma literatürden belirlenmeli; belirsizse "uncertain" etiketlenmeli.

## 5. ClinVar / ClinGen / gnomAD / GeneReviews — bulundu, aranıp bulunamadı, erişim başarısız

| Vaka | ClinVar (tam eşleşme) | ClinGen geçerlilik | ClinGen dozaj (HI) | gnomAD (pLI / LOEUF / mis_z) | GeneReviews |
|---|---|---|---|---|---|
| ACTL6B | **aranıp bulunamadı** (cDNA, transkript+cDNA, gen+cDNA, gen+protein; 4 terim) | küratörlük yok (tam liste 3717/3717 satır alındı) | 30 (otozomal resesif) | 7e-13 / 0.96 / 3.96 | gen haritasında bölüm yok; metin taramasında geçmiyor |
| ITPR1 | VCV000809404 Likely pathogenic (tek gönderici; durum "not provided") | SCA29 AD, ACAIDS AD/AR — Definitive | 1 | 1.0 / 0.36 / 7.40 | Hereditary Ataxia Overview (NBK1138, 2026-07-09) |
| SPTLC1 | **aranıp bulunamadı** | HSAN1A AD Definitive | kayıt yok | 2e-15 / 1.00 / 2.21 | NBK1390 (son revizyon **2021-12-02** → daha yeni kanıt PubMed/CT.gov'da aranmalı) |
| SYT1 | VCV000431484 Pathogenic/Likely pathogenic (çoklu gönderici, çatışmasız) | küratörlük yok | 1 | 1.0 / 0.32 / 4.22 | gen haritasında bölüm yok |
| CFTR (kontrol) | VCV000007105 Pathogenic (practice guideline) | CF AR Definitive | 30 | – | NBK1250 Cystic Fibrosis (2024-08-08) |

Bu çalıştırmada hiçbir kaynak "erişim başarısız" değildi; daha önceki bir denemede ClinGen geçici zaman aşımı yaşandı — bu tür durumlar artık ayrı sınıflanıyor ve raporda "retrieval failed, no conclusion" olarak yazılıyor. **Aranıp bulunamamak biyolojik/klinik yokluk anlamına gelmez** (ör. ACTL6B c.1099dup ClinVar'da olmayabilir ama literatürde raporlanmış olabilir; bu, LLM araştırma aşamasında PubMed'de aranmalı).

### Düzeltilen bilimsel hata (bu kontrollerle bulundu)
CFTR kontrolünde ClinVar'ın tırnaklı HGVS araması F508del için **başka varyantları** (c.1584+675A>G, c.1624G>T, c.350G>A, kompleks allel …) da döndürüyordu ve tam kayıt listenin sonundaydı. Eski kod tüm isabetleri bu varyantın kanıtı olarak ledger'a ekliyordu → yanlış atıf riski. Artık yalnızca başlığı aynı transkript erişiminde birebir aynı cDNA değişikliğini belirten kayıtlar kaydediliyor; diğerleri `non_matching_hits` olarak ayrı raporlanıyor (`tests/test_units.py::test_clinvar_exact_title_match`).

## 6. Klinik deneme retrieval'ı
Gen terimiyle açık durumlu çalışmalar alınabiliyor (ACTL6B: 1 gözlemsel; ITPR1: 1 nadir hastalık kayıt çalışması; SPTLC1: 1 girişimsel NCT07660614; SYT1: 1 girişimsel (henüz hasta almıyor) + 2 gözlemsel; CFTR: 25). Gen terimiyle bulunmak **uygunluk demek değildir**: tam uygunluk metni `get_trial` ile okunur, yaş/cinsiyet deterministik kontrol edilir, durum doğrulamada canlı tekrar sorgulanır. Gen terimi yalnızca başlangıçtır; hastalık/fenotip bazlı aramalar LLM araştırmacısının görevidir.

## 7. Kanıt defteri bütünlüğü, iddia doğrulama, şablon uyumu
- Ledger: kayıt birleştirme, anahtar normalleştirme, bilinmeyen atıfların temizlenmesi, geri çekilmiş kaynakların çıkarılması — birim testleriyle doğrulandı. Rapordaki her bağlantının ledger'dan geldiği QC ile kontrol ediliyor.
- İddia doğrulama: deterministik (PMID varlığı, canlı NCT durumu) + ayrı LLM doğrulayıcı (atomik iddialar + tam öğe metni). **Doğrulayıcının gerçek doğruluğu henüz ölçülmedi** (yalnızca mock ile akış test edildi).
- Şablon: başlıklar resmî şablon dosyasından türetiliyor; yapı doğrulayıcı organizatör form kontrollerini taklit ediyor ve mock çıktıyı reddediyor.

## 8. Açık bilimsel belirsizlikler
1. LLM'in varyanta özgü mekanizmayı (özellikle SPTLC1 HSAN1 vs ALS27, ITPR1 missense mekanizması, SYT1 dominant-negatif iddiası) doğru ayırt edip etmeyeceği bilinmiyor; ilk gerçek çalıştırmada uzman gözüyle incelenmeli.
2. Kanıt düzeyi sınıflandırması kısmen sezgisel (insan yayını tespiti yayın türü + kelime eşleşmesi).
3. Ücretli erişimli tam metinler okunamıyor; yalnızca özet ile doğrulanan iddialar `abstract_only` olarak işaretleniyor.
4. GeneReviews bölümleri eski olabilir (SPTLC1: 2021) — tarih rapora ve bağlama ekleniyor.
5. A/B/C karşılaştırması yapılmadan sistemin temel çizgiden daha iyi olduğu iddia edilemez.
