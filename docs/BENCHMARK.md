# Etaloninis testas (benchmark)

**Klausimas:** ar sistemos priežastinių įrodymų balas aukščiau iškelia taikinius, kurie realiai pasiekė
klinikinius tyrimus? Tai matavimo priemonė scoringo pakeitimams lyginti. Ji neįrodo klinikinės sėkmės.

**Kaip skaičiuojama** (`discovery-agent benchmark`):

1. Kiekvienai iš 20 dažnų ligų imami 500 Open Targets taikinių, surikiuotų **be klinikinių ir literatūros
   duomenų**. Jei klinikiniai duomenys rikiuotų sąrašą, į jį patektų taikiniai arba dėl genetikos, arba dėl
   vaistų. Tai sukurtų dirbtinę neigiamą koreliaciją (Berksono paradoksą): pirmoje versijoje dėl to buvo
   AUC 0,15.
2. Kiekvienas taikinys įvertinamas tik priežastiniais įrodymais. Tai genetika, somatinės mutacijos, modeliai,
   keliai ir raiška; klinika ir literatūra į balą neįeina, todėl testas neturi rato.
3. Teigiami pavyzdžiai yra taikiniai, kuriems ligai yra klinikinėje stadijoje esantis arba patvirtintas
   vaistas (atskira OT užklausa).
4. Matuojama:
   * AUC (0,5 = atsitiktinai);
   * tikslumas top-10 ir top-20;
   * kiek kartų top-10 dažniau randami klinikiniai taikiniai, nei atsitiktinai (lift).

**Išvados (2026-11-05, OT 26.09):**

* Numatytieji svoriai: AUC **0,61**, o top-10 klinikinius taikinius randa **4,8 karto** dažniau, nei
  atsitiktinai.
* Lygūs svoriai duoda geresnį bendrą AUC (0,66), bet prastesnį top-10 tikslumą. Kadangi sistema naudoja
  top sąrašą, numatytieji svoriai kol kas paliekami.
* Silpniausiai sekasi ligoms, kurių vaistai buvo kuriami ne iš genetikos: IPF 0,44, ALS 0,47, krūties
  karcinoma 0,48.
* Pakartokite testą po kiekvieno balų pakeitimo ir lyginkite su šia lentele.

---

## Rezultatai: causal-evidence score vs. clinical-stage targets

20 diseases; pool = top 500 associations ranked without clinical and literature evidence; positive = target with a clinical-stage or approved drug for the disease. AUC 0.5 = random.

## Summary (mean over diseases)

| Preset | AUC | P@10 | P@20 | Lift@10 |
|---|---|---|---|---|
| equal | 0.656 | 0.20 | 0.18 | 4.39 |
| default | 0.613 | 0.23 | 0.16 | 4.82 |
| no_expression | 0.610 | 0.22 | 0.16 | 4.66 |
| genetics_only | 0.578 | 0.23 | 0.15 | 4.76 |

## Per disease (AUC)

| Disease | Targets | Clinical | default | genetics_only | equal | no_expression |
|---|---|---|---|---|---|---|
| idiopathic pulmonary fibrosis | 494 | 12 | 0.44 | 0.39 | 0.52 | 0.44 |
| type 2 diabetes mellitus | 500 | 28 | 0.55 | 0.46 | 0.62 | 0.55 |
| rheumatoid arthritis | 498 | 31 | 0.64 | 0.61 | 0.72 | 0.64 |
| Crohn disease | 492 | 17 | 0.61 | 0.59 | 0.67 | 0.59 |
| ulcerative colitis | 493 | 15 | 0.72 | 0.71 | 0.76 | 0.71 |
| asthma | 492 | 22 | 0.71 | 0.71 | 0.72 | 0.71 |
| psoriasis | 498 | 26 | 0.63 | 0.61 | 0.57 | 0.62 |
| systemic lupus erythematosus | 497 | 21 | 0.55 | 0.52 | 0.64 | 0.55 |
| multiple sclerosis symptom measurement | 500 | 39 | 0.60 | 0.49 | 0.67 | 0.60 |
| Alzheimer disease | 497 | 50 | 0.56 | 0.48 | 0.67 | 0.55 |
| Parkinson disease | 496 | 45 | 0.59 | 0.46 | 0.63 | 0.58 |
| amyotrophic lateral sclerosis | 493 | 25 | 0.47 | 0.49 | 0.47 | 0.47 |
| schizophrenia | 499 | 34 | 0.46 | 0.40 | 0.64 | 0.46 |
| coronary artery disorder | 499 | 24 | 0.66 | 0.64 | 0.68 | 0.66 |
| hypertensive disorder | 500 | 32 | 0.62 | 0.60 | 0.65 | 0.62 |
| chronic obstructive pulmonary disease | 498 | 27 | 0.72 | 0.71 | 0.65 | 0.72 |
| atopic eczema | 499 | 16 | 0.79 | 0.77 | 0.80 | 0.79 |
| osteoarthritis | 499 | 10 | 0.57 | 0.56 | 0.63 | 0.57 |
| breast carcinoma | 489 | 54 | 0.48 | 0.45 | 0.51 | 0.48 |
| Hypercholesterolemia | 499 | 7 | 0.90 | 0.90 | 0.90 | 0.90 |
