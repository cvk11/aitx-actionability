# Mimari kararlar: tutorial karşılaştırması ve yeniden değerlendirme (Part 3–4)

## 1. Organizatörlerin önerdiği yaklaşım (https://aitxchallenge.org/tutorial/ ve FAQ)

| Konu | Organizatör önerisi | Bizim uygulamamız | Fark / iyileştirme |
|---|---|---|---|
| Ajan yapısı | "pick a tool, see the result, decide what to do next" çekirdek döngüsü (Anthropic "Building effective agents"); bölümler ayrı araştırılıp birleştirilebilir | Bölüm grubuna özgü 3 paralel Claude araç döngüsü (Tier 1 / 2 / 3) + bir yorumlayıcı | Aynı ilke; ek olarak deterministik ön tarama (bootstrap) ve sınırlı tur bütçesi |
| Veri erişimi | MCP ile kaynaklar; örnek: embedding'li CT.gov ve PubMed MCP Space'leri | Doğrudan resmî REST API'ler (client-side tools, katı şema) | MCP yerine doğrudan araç: daha az hareketli parça; embedding indeksleri yerine canlı kayıt (FAQ'nin "pull the live record" uyarısına doğrudan uyum) |
| Agentic retrieval | Plan → canlı API → oku → tekrar sorgula; önceden indeks yalnızca aday bulmak için | Aynı; GeneReviews tek istisna (resmî arşiv anlık görüntüsü, tarih+hash, çünkü canlı sayfa bot korumalı) | Anlık görüntünün revizyon tarihi raporda/bağlamda belirtiliyor |
| Şablon uyumu | "Use a simple script (rather than tokens) to walk the headings" | Başlıklar koddan; deterministik render + QC | Tutorial'dan bir adım ileri: başlığı LLM hiç üretmiyor |
| Atıf temellendirme | Long-form factuality (atomik olgular), her iddiayı kaynağıyla üret, ayrı doğrulama geçişi; başarısızsa düşür/yumuşat; Claude citations | Atomik iddialar + kaynak metinle LLM doğrulama + deterministik varlık/durum kontrolü + kural tabanlı tier denetimi; desteklenmeyen düşürülür/nitelenir | Claude "citations" özelliği kullanılmadı: yapılandırılmış çıktılarla (output_config.format) birlikte kullanılamıyor (API 400); bunun yerine ledger anahtarı zorunluluğu |
| Bütünsel değerlendirme | Güçlü bir modelle 5 boyutta yerel puanlama (LLM-as-judge) | `Z_judge` (isteğe bağlı) + deterministik heuristik puan kartı, "development heuristic" etiketiyle | Rapor içeriğini değiştirmiyor (doğrulanamayan geri bildirime göre otomatik düzenleme yok) |
| Dağıtım | HF Space, Gradio | Gradio `api_name="/actionability_report"` + FastAPI `POST` | Faz-1 doğrulayıcısının `gradio_client.predict` çağrı biçimi test edildi |

## 2. Önerilen mimarinin (Claude araştırmacı → retrieval → doğrulama → Gemini → rapor) eleştirel değerlendirmesi

| Soru | Karar | Gerekçe |
|---|---|---|
| Ajanlar birleştirilmeli mi? | **B ve C birleştirildi** (retrieval ve terapötik akıl yürütme aynı döngüde), ama **bölüm grubuna göre 3'e ayrıldı** | Ayrı "retrieval ajanı" + "strateji ajanı" bilgi kaybına ve kanıt aramadan akıl yürütmeye yol açar; bölüm bazlı ayrım tutorial'ın önerisi ve odaklı prompt sağlar |
| Ek özel araçlar gerekli mi? | **Evet:** RefSeq ekson modeli + NMD kuralları, VariantValidator, VEP, ClinGen, gnomAD, UniProt pozisyon özellikleri, GeneReviews arşivi | Bespoke bölümü N=1 Collaborative uzmanlarınca okunacak; ekson çerçevesi/NMD/alan bilgisi olmadan ASO uygunluk hükmü tahmin olur |
| Bağımsız iddia doğrulama puanı artırır mı? | **Evet, zorunlu** (ama ablation `B_no_verification` ile ölçülecek) | Resmî kural: yanlış/yanıltıcı ifade, doğru içerik olsa bile negatif puan; tutorial "plausible-looking citation that says something subtly different" uyarısı |
| Çoklu model incelemesi değer mi? | **Güncelleme (2026-11-02): Gemini kullanılmıyor; aktif model yalnızca Claude Opus.** İnceleme aşaması (E) koda bağlı kalıyor ama hiçbir yapılandırma bir gözden geçirici oluşturmuyor. Önceki karar: isteğe bağlı, varsayılan kapalı | Maliyet/karmaşıklık artışı ölçülmeden açılmamalı; Gemini görüşü asla doğrudan uygulanmaz, kanıta dayalı hakemlik + takip araştırması gerekir. Kullanıcının yeni planında C kolu "bağımsız OpenAI araştırması" — henüz uygulanmadı (yer tutucu) |
| Varyant-mekanizma doğrulama aşaması zorunlu mu? | **Evet** (yorumlayıcı aşaması + deterministik özellikler + mekanizma güven etiketi) | Aynı gende farklı mekanizmalar (örn. UniProt'ta SPTLC1 kalıntı 39 için ALS27 notu) tedavi yönünü tersine çevirebilir |
| Kalite retrieval'dan mı akıl yürütmeden mi gelir? | **Önce retrieval ve doğrulama**; akıl yürütme `effort` ile ayarlanır | Puanlama doğrulanabilir kanıtı ödüllendiriyor; ek akıl yürütme kanıt yokluğunu telafi etmez |
| Daha basit mimari daha güvenilir olabilir mi? | **Mümkün — bu yüzden A kolu (tek ajan temel çizgi) uygulandı** | Sistemin karmaşıklığı ancak A'ya karşı ölçülebilir üstünlükle meşrulaşır |

## 3. Nihai akış
```
input validation → deterministic bootstrap (variant model, VV, VEP, ClinVar exact-match, ClinGen, gnomAD, UniProt, GeneReviews, trials)
→ A interpret (mechanism, gene vs variant, missing info) → B/C research ×3 (parallel agentic loops, strict tools)
→ deterministic rules (citations ∈ ledger, retracted, trial status/age/sex, off-label precedent, targeted needs label/guideline)
→ D verify (PMID existence, live NCT status, LLM claim + statement entailment) → [E review + adjudication + follow-up]
→ bounded revision (≤ AITX_MAX_REVISIONS; re-verification of changed items; unresolved statements dropped)
→ F synthesis (summary per tier, conditional missing-information actions, Tier-3 outcome measures & ranking)
→ deterministic render → structural QC + ledger link check → output {id, report_markdown} + audit trail
```
