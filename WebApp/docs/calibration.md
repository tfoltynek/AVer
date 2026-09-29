# Kalibrace pravděpodobností

## Oprava 29. 9. 2026

Jednoslovné odpovědi se nadále hodnotí přesnou shodou po odstranění okolních
mezer a převodu na malá písmena. Diakritika a tvar slova se zachovávají.
Víceslovné odpovědi se nadále hodnotí podobností s prahem 0,7172.

Dodané hodnoty z Hitzingerova e-mailu a notebooku `pA_pN_computing.ipynb`
počítají s prahem podobnosti i u jednoslovných odpovědí. Dřívější hodnoty
aplikace navíc zahrnovaly jednoslovné odpovědi bez filtru pozice modelu.
Ani jedna varianta proto neodpovídala přesnému hodnocení v aplikaci.

Pro dvě skupiny jednoslovných výrazů vybraných modelem byly hodnoty přepočteny
z původního CSV. Výběr podskupin zůstává stejný jako v dodaném notebooku:
`selector in {ml, unigram}`, příslušný slovní druh a `1 < ml_pos <= 40`.
Autoři mají `test_type == authorML`, ostatní role tvoří skupinu neautorů.
Změněno je kritérium správné odpovědi na přesnou shodu používanou aplikací.

| Skupina | Správně / celkem autorů | Správně / celkem neautorů | pA | pN |
|---|---|---|---|---|
| Podstatná jména | 121 / 170 | 130 / 325 | 0,71176 | 0,40000 |
| Přídavná jména | 28 / 41 | 16 / 71 | 0,68293 | 0,22535 |

Hodnoty aplikace jsou zaokrouhlené na pět desetinných míst. Jde o počty
odpovědí, nikoli počty nezávislých účastníků.

## Reprodukce

CSV obsahuje 6 515 odpovědí a má SHA-256:

```text
198c76740b4fdd6cb72170a359eda7eb100a0b18e08ba9bcf908973cb19824d3
```

Soukromé CSV není součástí repozitáře. Přepočet vyžaduje jeho lokální kopii:

```sh
python scripts/audit_calibration.py /cesta/k/data_full.csv
```

Skript vypíše pouze souhrnné počty, pravděpodobnosti a hash vstupu.
Vedle přesné shody reprodukuje i původní výpočet podle podobnosti:
podstatná jména 142/170 a 167/325; přídavná jména 32/41 a 24/71.
Ty odpovídají e-mailovým hodnotám 0,83529/0,51385 a 0,78049/0,33803.

## Rozsah a omezení

- Oprava mění dvě kalibrační dvojice. Nemění způsob hodnocení odpovědí,
  výběr výrazů ani kalibraci ostatních skupin.
- Hodnoty 0,51163/0,12840 a 0,53468/0,20450 patří podle notebooku
  **trigramům**; obě podskupiny mají filtr `word_counts == 3`.
  Dokumentace ověření je nesmí označovat jako bigramy.
- Současnou společnou dvojici pro bigramy 0,56110/0,34434 lze reprodukovat
  ze stejného CSV: 326/581 autorských a 688/1998 neautorských odpovědí
  dosahuje podobnosti alespoň 0,7172.
- Notebook používá `1 < ml_pos <= 40`. API README uvádí nulově indexované
  pořadí `1 <= rank < 41`. Shoda číslování historického CSV s API není
  doložena. Oprava zachovává výzkumný vzorek notebooku; neprohlašuje,
  že se jeho výběrový filtr přesně shoduje se současným API.
- Trigramové podskupiny notebooku se překrývají, zatímco aplikace přiřazuje
  jednu skupinu podle priority. Jejich hodnoty ani priorita se touto opravou
  nemění. Hodnoty pro náhodný výběr pocházejí z jiné kalibrace a zde nebyly
  znovu ověřeny. Toto není nová validace celé metody ani všech nasazení.

Pravděpodobnost se počítá při zobrazení výsledku, takže po nasazení budou nové
dvojice použity i pro dříve vyplněné testy. Uložená správnost odpovědí se
nemění; není potřeba spouštět `rescoretests` ani databázovou migraci.
