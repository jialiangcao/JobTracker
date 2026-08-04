"""Non-regex filters applied after the DB rule set: posting age and US location.

These live in code rather than in `filter_rules` because neither is expressible as a
regex over one field. Age needs arithmetic against the current date, and location needs
to reconcile the wildly inconsistent strings boards actually emit — "Chicago, IL",
"Chicago, United States", "US / Canada", "Toronto, New York, San Francisco", and (for
boards that put the work model where the city belongs) "In-Office", with the real city
only in the title.

Both are fail-open: a posting with no date, or with no recognizable geography anywhere,
is kept. Missing a real internship costs more than showing one extra.
"""

import re
from datetime import UTC, datetime, timedelta

from jobtrack.schema import JobPosting

# State abbreviations only count when comma- or delimiter-preceded ("Portland, OR"), so
# the "OR" in "Dublin OR London" doesn't read as Oregon. Case-sensitive: postings write
# them uppercase, and lowercase "us"/"in" appear constantly in prose.
_US_ABBREV = re.compile(
    r"(?:^|[,;/•|])\s*(?:A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[DLAN]|K[SY]|LA|M[DAIENSOT]"
    r"|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])(?=$|[,;/•|)\s])"
)

# The country token is a *strong* signal, unlike the state abbreviations above: "US /
# Canada" is a US posting even though a non-US country is named. Kept separate so it is
# checked in the same tier as _US_NAME rather than last.
_US_COUNTRY = re.compile(r"\bU\.?S\.?A?\.?(?=$|[^\w.])")

_US_NAME = re.compile(
    r"united states"
    # "Baja California" and "Baja California Sur" are Mexican states whose names contain a
    # US state name, so California needs an explicit guard the others don't.
    r"|(?<!baja )\bcalifornia\b"
    r"|\b(?:alabama|alaska|arizona|arkansas|colorado|connecticut|delaware"
    r"|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana"
    r"|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana"
    r"|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina"
    r"|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina"
    r"|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia"
    r"|wisconsin|wyoming)\b"
    r"|\b(?:san francisco|nyc|new york city|brooklyn|seattle|austin|chicago|boston"
    r"|los angeles|atlanta|denver|boulder|dallas|houston|philadelphia|phoenix"
    r"|san diego|san mateo|mountain view|palo alto|menlo park|sunnyvale|santa clara"
    r"|bellevue|redmond|cupertino|arlington|mclean|pittsburgh|detroit|ann arbor"
    r"|minneapolis|madison|miami|portland|salt lake city|raleigh|durham|charlotte"
    r"|nashville|columbus|kansas city|st\.? louis|las vegas|sacramento|irvine"
    r"|jersey city|hoboken|stamford|princeton|bentonville"
    r"|remote\s*[-\u2013\u2014(]*\s*us)\b",
    re.IGNORECASE,
)

# Trailing ISO-3166 alpha-2 country codes, the form Workday/SmartRecruiters tenants emit:
# "Aveiro, pt", "Tan Binh, vn", "BangPa-in, th". Deliberately omits every code that
# collides with a US state abbreviation (CA/DE/IN/PA/MA/MD/MT/NE/CO/GA/ID/IL/LA/MO/MS/
# NC/SC/SD/VA/KY/AZ/TN/MN/AL/AR), so "Boston, MA" can never read as Morocco. Those
# ambiguous codes are left to the city-name list and the Canada rule below.
_NON_US_CC = re.compile(
    r"(?:^|[,;/•|])\s*(?:AE|AF|AM|AT|AU|BD|BE|BG|BH|BR|BY|CH|CL|CM|CN|CR|CU|CY|CZ|DK|DO"
    r"|DZ|EC|EE|EG|ES|ET|FI|FR|GB|GE|GH|GR|GT|HK|HR|HU|IE|IQ|IR|IS|IT|JM|JO|JP|KE|KH|KR"
    r"|KW|KZ|LB|LK|LT|LU|LV|MC|MM|MX|MY|NG|NL|NP|NZ|OM|PE|PH|PK|PL|PT|PY|QA|RO|RS|RU|SA"
    r"|SE|SG|SI|SK|TH|TR|TW|TZ|UA|UK|UY|UZ|VE|VN|ZA|ZM|ZW)(?=$|[,;/•|)\s])",
    re.IGNORECASE,
)

# Workday site codes lead with an uppercase country code and a hyphen — "IT-BA-BARI-STRADA
# PROVINCIALE". Anchored at the start and case-sensitive so a lowercase word beginning with
# these letters can't trigger it. The US equivalent ("US-NC-Charlotte") is absent from the
# code list, so it correctly falls through to the US branches.
#
# A "CAN"/"CANHQ" prefix is deliberately NOT treated as Canada: Carrier uses "CANHQ: CCS HQ
# - Syracuse … NY … USA" as a site code, so the rule would drop real US postings — the one
# error this module is built to avoid.
_NON_US_SITE_CODE = re.compile(
    r"^(?:AE|AF|AM|AT|AU|BD|BE|BG|BH|BR|BY|CH|CL|CM|CN|CR|CU|CY|CZ|DK|DO|DZ|EC|EE|EG|ES"
    r"|ET|FI|FR|GB|GE|GH|GR|GT|HK|HR|HU|IE|IQ|IR|IS|IT|JM|JO|JP|KE|KH|KR|KW|KZ|LB|LK|LT"
    r"|LU|LV|MC|MM|MX|MY|NG|NL|NP|NZ|OM|PE|PH|PK|PL|PT|PY|QA|RO|RS|RU|SA|SE|SG|SI|SK|TH"
    r"|TR|TW|TZ|UA|UK|UY|UZ|VE|VN|ZA|ZM|ZW)-"
)

# "Milton, Ontario, CA" — the CA is Canada, but _US_ABBREV reads it as California and the
# US-wins rule then keeps the posting. A Canadian province sitting between two delimiters
# immediately before the CA disambiguates it. Requiring the leading delimiter is what
# preserves "Ontario, CA" on its own, which really is Ontario, California.
# Province names match case-insensitively; the two-letter codes are pinned case-sensitive
# with (?-i:…) so the preposition "on" and the country code "ca" in lowercase prose can't
# stand in for Ontario and Canada.
_CANADA_CC = re.compile(
    r"[,;/•|]\s*(?:ontario|quebec|québec|british\s+columbia|alberta|manitoba|saskatchewan"
    r"|nova\s+scotia|new\s+brunswick|newfoundland|prince\s+edward\s+island|yukon|nunavut"
    r"|northwest\s+territories|(?-i:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU))"
    r"\s*[,;/•|]\s*(?-i:CA)(?=$|[,;/•|)\s])",
    re.IGNORECASE,
)

# Deliberately omits names that are also US places — Cambridge, Manchester, Birmingham,
# Ontario, Athens, Lima, Valencia, Wellington — to avoid excluding US postings.
_NON_US = re.compile(
    r"\b(?:canada|toronto|vancouver|montreal|ottawa|calgary|waterloo|kanata|mississauga"
    r"|brampton|markham|etobicoke|scarborough|kitchener|guelph|oakville|burlington, on"
    r"|edmonton|winnipeg|halifax|saskatoon|regina, sk|victoria, bc|laval|gatineau"
    r"|united kingdom|england|scotland|wales|london|edinburgh|glasgow|belfast|bristol"
    r"|ireland|dublin|cork"
    r"|germany|berlin|munich|münchen|hamburg|frankfurt|cologne|stuttgart"
    r"|france|paris|lyon|toulouse|nantes"
    r"|netherlands|amsterdam|rotterdam|utrecht|eindhoven"
    r"|spain|madrid|barcelona|italy|rome|roma|milan|milano|turin|torino|bologna|bari"
    r"|manching|renningen|weissach|böblingen|schweinfurt|kassel|siegen|paderborn"
    r"|sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki"
    r"|poland|warsaw|kraków|krakow|wrocław|wroclaw|gdansk"
    r"|czech|czechia|prague|hungary|budapest|romania|bucharest|cluj"
    r"|bulgaria|sofia|serbia|belgrade|croatia|zagreb|slovakia|bratislava|slovenia"
    r"|portugal|lisbon|porto|greece|switzerland|zurich|zürich|geneva|lausanne"
    r"|austria|vienna|belgium|brussels|ghent|luxembourg|iceland|reykjavik"
    r"|ukraine|kyiv|kiev|lithuania|vilnius|latvia|riga|estonia|tallinn"
    r"|israel|tel aviv|jerusalem|haifa|turkey|istanbul|ankara"
    r"|india|bengaluru|bangalore|mumbai|new delhi|gurugram|gurgaon|noida|hyderabad"
    r"|pune|chennai|kolkata|ahmedabad|jaipur"
    r"|singapore|japan|tokyo|osaka|kyoto|china|beijing|shanghai|shenzhen|guangzhou"
    r"|hong kong|taiwan|taipei|korea|seoul|vietnam|hanoi|ho chi minh"
    r"|thailand|bangkok|indonesia|jakarta|philippines|manila|malaysia|kuala lumpur"
    r"|pakistan|karachi|lahore|islamabad|bangladesh|dhaka|sri lanka|colombo"
    r"|australia|sydney|melbourne|brisbane|canberra|adelaide|new zealand|auckland"
    r"|brazil|são paulo|sao paulo|rio de janeiro|belo horizonte"
    r"|mexico|guadalajara|monterrey|baja california|argentina|buenos aires|chile|santiago"
    r"|colombia|bogotá|bogota|medellín|medellin|peru|uruguay|montevideo|costa rica"
    r"|united arab emirates|uae|dubai|abu dhabi|saudi arabia|riyadh|qatar|doha"
    r"|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|cape town|johannesburg"
    r"|morocco|tunisia|ghana|accra"
    r"|emea|apac|latam|anz"
    # Second-tier cities that show up constantly on enterprise tenants and were slipping
    # through fail-open because no country name appeared anywhere in the string.
    r"|suzhou|hangzhou|chengdu|chongqing|wuhan|nanjing|tianjin|qingdao|dalian|shenyang"
    r"|wuxi|ningbo|zhuhai|xiamen|changsha|zhengzhou|hefei|jinan|foshan|dongguan"
    r"|oberkochen|jena|dresden|leipzig|nuremberg|nürnberg|düsseldorf|dusseldorf|essen"
    r"|dortmund|bremen|hannover|hanover|karlsruhe|mannheim|heidelberg|aachen|bonn"
    r"|wolfsburg|ingolstadt|regensburg|erlangen|böblingen|boblingen|sindelfingen"
    r"|friedrichshafen|leverkusen|ludwigshafen|walldorf|darmstadt|braunschweig"
    r"|aveiro|braga|coimbra|guimarães|guimaraes"
    r"|tan binh|da nang|danang|hai phong|haiphong|bien hoa|can tho"
    r"|bang\s*pa[-\s]?in|chonburi|rayong|ayutthaya|pathum thani|samut prakan"
    r"|graz|linz|salzburg|innsbruck|basel|bern|lugano|winterthur"
    r"|antwerp|leuven|liege|namur|delft|groningen|nijmegen|enschede|arnhem"
    r"|malmö|malmo|gothenburg|göteborg|uppsala|lund|aarhus|odense|trondheim|bergen"
    r"|tampere|espoo|turku|oulu|vantaa"
    r"|poznań|poznan|łódź|lodz|katowice|gdynia|szczecin|lublin|rzeszów|rzeszow"
    r"|brno|ostrava|plzen|plzeň|košice|kosice|timisoara|timișoara|iasi|iași|brasov"
    r"|thessaloniki|patras|valletta|nicosia|limassol"
    r"|coimbatore|mysuru|mysore|kochi|cochin|indore|nagpur|vadodara|surat|bhubaneswar"
    r"|thiruvananthapuram|trivandrum|mohali|chandigarh|vizag|visakhapatnam"
    r"|cebu|davao|penang|johor|selangor|batam|surabaya|bandung|medan"
    r"|nagoya|yokohama|fukuoka|sapporo|kobe|sendai|hiroshima"
    r"|busan|incheon|daejeon|daegu|suwon|seongnam|pangyo"
    r"|hsinchu|kaohsiung|taichung|tainan|macau|macao"
    r"|perth|hobart|wollongong|newcastle,\s*au|christchurch|wellington,\s*nz"
    r"|campinas|curitiba|porto alegre|brasília|brasilia|recife|fortaleza|florianópolis"
    r"|querétaro|queretaro|tijuana|puebla|mérida|merida|hermosillo|aguascalientes"
    r"|córdoba,\s*ar|rosario|valparaíso|concepción|barranquilla|cali,\s*co"
    r"|san josé,\s*cr|san jose,\s*cr|heredia|alajuela"
    r"|casablanca|rabat|alexandria,\s*eg|pretoria|durban|stellenbosch"
    r"|sharjah|jeddah|dammam|manama|kuwait city|muscat|amman|beirut"
    r"|belfast|derry|swansea|cardiff|aberdeen|dundee|coventry|leicester|nottingham"
    r"|sheffield|leeds|liverpool|newcastle upon tyne|brighton|reading,\s*uk|slough"
    r"|milton keynes|basingstoke|swindon|warrington|staines|farnborough"
    r"|galway|limerick|waterford,\s*ie"
    r"|bordeaux|lille|grenoble|montpellier|rennes|strasbourg|marseille|nice,\s*fr"
    r"|sophia antipolis|toulon|clermont-ferrand"
    r"|bologna|florence|firenze|naples|napoli|genoa|genova|padua|padova|verona|catania"
    r"|bilbao|seville|sevilla|zaragoza|málaga|malaga|alicante|vigo"
    r"|tel-aviv|herzliya|ra'anana|raanana|petah tikva|beer sheva|netanya"
    r"|izmir|bursa|antalya|gebze|kocaeli"
    r"|lviv|kharkiv|dnipro|odesa|odessa|minsk|almaty|astana|tashkent|baku|tbilisi|yerevan"
    r"|karachi|rawalpindi|faisalabad|chittagong|kathmandu"
    r")\b",
    re.IGNORECASE,
)


# Workday emits non-breaking spaces inside location strings ("Ho\xa0Chi\xa0Minh\xa0City"),
# which silently defeats every multi-word entry in the patterns above — the posting then
# falls through to fail-open and reads as US. Collapse all Unicode spacing to plain spaces
# before matching.
_SPACES = re.compile(r"[\s\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000\ufeff]+")


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def is_recent(job: JobPosting, max_age_days: int, now: datetime | None = None) -> bool:
    """False only when the posting has a date and that date is older than the window."""
    if max_age_days <= 0 or job.posted_at is None:
        return True
    return job.posted_at >= (now or datetime.now(UTC)) - timedelta(days=max_age_days)


def is_us_location(job: JobPosting) -> bool:
    """Location first; fall back to the title for boards that put the work model
    ("In-Office", "Hybrid") in the location field and the city in the title.

    Signals are checked strongest-first rather than US-first, because the weakest US
    signal — a bare two-letter abbreviation — is the one that misfires:

      1. an explicit non-US country code ("Aveiro, pt") or a Canadian "…, CA"
      2. a full US place name or the US country token ("San Francisco", "US / Canada")
      3. a non-US place name ("Suzhou", "Oberkochen")
      4. a bare US state abbreviation ("Boise, ID")

    A full US name still beats a non-US one, so multi-site listings like "Toronto, New
    York, San Francisco" are kept. But an abbreviation no longer beats a foreign city,
    which is what made "Milton, Ontario, CA" read as California. The remaining cost is
    that a US city name inside a foreign location ("San José, Costa Rica") reads as US.
    """
    for raw, is_location in ((job.location or "", True), (job.title, False)):
        text = _normalize(raw)
        if not text:
            continue
        # Bare country codes are a location-string convention only. Applied to titles they
        # misread "IT Intern" as Italy and "SE Intern" as Sweden.
        if is_location and (
            _NON_US_CC.search(text) or _CANADA_CC.search(text) or _NON_US_SITE_CODE.search(text)
        ):
            return False
        if _US_NAME.search(text) or _US_COUNTRY.search(text):
            return True
        if _NON_US.search(text):
            return False
        if _US_ABBREV.search(text):
            return True
    return True


def is_eligible(
    job: JobPosting, *, max_age_days: int, us_only: bool, now: datetime | None = None
) -> bool:
    if not is_recent(job, max_age_days, now):
        return False
    return not (us_only and not is_us_location(job))
