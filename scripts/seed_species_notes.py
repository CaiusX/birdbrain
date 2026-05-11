"""One-off: seed curated species notes for everything BirdNET has detected so
far, with my best ornithologically-informed take on each species' plausibility
given the biome (Tembe sand-forest in northern KZN, Olifants riverine Kruger,
wild-africa-live generic Kruger). Tags drive the colored banner in the
audition modal: 'reliable' / 'suspect' / 'rare' / None.

Reasoning logic: I weight (a) the biome fit, (b) max confidence achieved,
(c) BirdNET's known confusables, and (d) the volume of detections. A species
with a high count but middling max-conf is much more suspect than a few
high-conf hits.

Run with: uv run python scripts/seed_species_notes.py
"""

from africam.config import AppConfig
from africam.storage import Database

NOTES: list[tuple[str, str, str, str | None]] = [
    # --- High count, common-sense species --------------------------------------
    ("Pycnonotus barbatus", "Common Bulbul",
     "BirdNET reports this for any southern African bulbul; locally it is almost "
     "certainly Dark-capped Bulbul (P. tricolor) — the model uses lumped taxonomy. "
     "Treat as Dark-capped Bulbul, which is very common and reliable in both habitats.",
     "reliable"),
    ("Oriolus larvatus", "African Black-headed Oriole",
     "Loud whistled 'fee-oo-fee-oo' / 'pee-ho-piii' from canopy. Common in "
     "broadleaved woodland and riverine fringe; high-conf detections are reliable.",
     "reliable"),
    ("Telophorus viridis", "Four-colored Bushshrike",
     "Now usually placed in Chlorophoneus. Whistled couplets 'wreee-tew-tew' from "
     "thick cover. Eastern lowveld and sand-forest specialist — Tembe is core "
     "habitat. Reliable when max conf ≥0.9.",
     "reliable"),
    ("Cisticola erythrops", "Red-faced Cisticola",
     "Loud triple 'tweep-tweep-tweep' from reed and rank vegetation near water. "
     "Atypical in deep Tembe sand-forest; high count there is worth auditioning.",
     None),
    ("Streptopelia semitorquata", "Red-eyed Dove",
     "Characteristic 'I am, a red, eyed dove' coo — six syllables. Common woodland "
     "edge across the region; reliable in both biomes.",
     "reliable"),
    ("Fraseria plumbea", "Gray Tit-Flycatcher",
     "Older lit calls this Plain Tit-Flycatcher (now split). Quiet whistled phrases, "
     "lowveld and sand-forest. Plausible at Tembe; ambient counts make sense.",
     "reliable"),
    ("Calidris minuta", "Little Stint",
     "Palaearctic migrant; soft 'chit' on take-off. Plausible at olifants riverbanks "
     "in austral summer, but 40+ rows is high — BirdNET may be triggering on similar "
     "peeping calls from other waders. Audit before trusting volume.",
     None),
    ("Malaconotus blanchoti", "Gray-headed Bushshrike",
     "Mournful sustained 'hooooop' whistle — the 'ghost bird'. Skulker but very vocal. "
     "Sand-forest / dense thicket — Tembe is good habitat.",
     "reliable"),
    ("Cercotrichas leucophrys", "Red-backed Scrub-Robin",
     "Sometimes split as White-browed Scrub-Robin. Varied chattering song from "
     "thickets. Common lowveld species; max conf 0.63 here is mid-range — audit a "
     "sample to confirm.",
     None),
    ("Chloropicus namaquus", "Bearded Woodpecker",
     "Loud rapid drumming and a distinctive 'wik-wik-wik' call. Open lowveld with "
     "tall trees — fits both sites.",
     "reliable"),
    ("Merops bullockoides", "White-fronted Bee-eater",
     "Highly social; 'wherrp' contact calls from earth-bank colonies along rivers. "
     "Olifants riverbank is textbook habitat. Reliable.",
     "reliable"),
    ("Motacilla flava", "Western Yellow Wagtail",
     "Palaearctic migrant present roughly Oct–Apr. Sharp 'tsweep' calls. Plausible "
     "at olifants in summer; reliable IDs warrant checking the date.",
     "reliable"),
    ("Caprimulgus pectoralis", "Fiery-necked Nightjar",
     "Classic dusk-and-dawn whistled phrase often transcribed 'Good-Lord-deliver-us'. "
     "Lowveld common; max 0.99 is unambiguous.",
     "reliable"),
    ("Halcyon albiventris", "Brown-hooded Kingfisher",
     "Loud rattled 'kik-kik-kik-kik' descending. Common woodland kingfisher away "
     "from water; reliable.",
     "reliable"),
    ("Laniarius ferrugineus", "Southern Boubou",
     "Bell-like antiphonal duets from thickets. Reliable in both habitats.",
     "reliable"),
    ("Zapornia flavirostra", "Black Crake",
     "Throaty rolling 'krrrrk' and rapid chuckling duets from reed margins. "
     "Plausible at olifants margins; high conf reliable.",
     "reliable"),
    ("Accipiter tachiro", "African Goshawk",
     "Dawn display call: a regular 'chip... chip... chip' delivered while flying "
     "high. Distinctive and reliable.",
     "reliable"),
    ("Apalis flavida", "Yellow-breasted Apalis",
     "Brisk repeated 'chip-pee-chip-pee' duets from mid-canopy. Common in mixed "
     "broadleaved woodland.",
     "reliable"),
    ("Batis molitor", "Chinspot Batis",
     "Three-note descending whistle, often rendered 'three blind mice'. Reliable "
     "in mixed woodland.",
     "reliable"),
    ("Turtur chalcospilos", "Emerald-spotted Wood-Dove",
     "Diagnostic descending 'du-du-du-du-du-du' cooing, slowing toward the end. "
     "Lowveld common.",
     "reliable"),
    ("Uraeginthus bengalus", "Red-cheeked Cordonbleu",
     "BirdNET often confuses this with Southern Cordonbleu (U. angolensis), which "
     "is the regular southern African species. East/Central African range — at "
     "Kruger latitudes the detections are almost certainly Southern Cordonbleu.",
     "suspect"),
    ("Scopus umbretta", "Hamerkop",
     "Yelping 'wiek-wiek' calls; almost always near water. Reliable at olifants.",
     "reliable"),
    ("Urocolius indicus", "Red-faced Mousebird",
     "Descending whistled 'tiwiririririi' from flight or treetop. Reliable in both "
     "savanna and gardens.",
     "reliable"),
    ("Pternistis natalensis", "Natal Francolin",
     "Cackling territorial song at dawn from rocky thicket. Reliable in lowveld.",
     "reliable"),
    ("Ceryle rudis", "Pied Kingfisher",
     "Rattling 'kit-kit-kit' calls from over water. Olifants riverbank is core "
     "habitat. Reliable.",
     "reliable"),
    ("Melaniparus niger", "Southern Black-Tit",
     "Buzzy 'chrr-chrr' churring calls + harsh chatter. Mixed broadleaved woodland.",
     "reliable"),
    ("Nilaus afer", "Brubru",
     "Sustained 'tirrr-rrr-rrr' trill — duets between male and female from canopy. "
     "Lowveld common.",
     "reliable"),
    ("Prinia subflava", "Tawny-flanked Prinia",
     "Repeated buzzy 'prrrt prrrt prrrt' from rank growth. Reliable wherever there "
     "is dense low cover.",
     "reliable"),
    ("Tringa glareola", "Wood Sandpiper",
     "Palaearctic migrant; high 'chiff-iff-iff' calls in flight. Common austral "
     "summer wader on flooded margins — fits olifants.",
     "reliable"),
    ("Uraeginthus angolensis", "Southern Cordonbleu",
     "The southern African cordonbleu (vs Red-cheeked, which is the East African "
     "form BirdNET sometimes mis-labels). Soft tinkling calls — reliable here.",
     "reliable"),
    ("Corythaixoides concolor", "Gray Go-away-bird",
     "Nasal 'ke-waaaay' from canopy — onomatopoeic name. Dry woodland and savanna. "
     "Reliable.",
     "reliable"),
    ("Sternula albifrons", "Little Tern",
     "Coastal seabird. Olifants is well inland — strongly suspect, almost certainly "
     "BirdNET reaching for a poor match. Max conf 0.56 supports the doubt.",
     "suspect"),
    ("Anthus trivialis", "Tree Pipit",
     "Palaearctic migrant; song-flight whistles in open woodland. Plausible in "
     "summer but mid-range conf — audit a sample.",
     None),
    ("Gymnoris superciliaris", "Yellow-throated Bush Sparrow",
     "Soft 'chip' calls from open lowveld bush. Plausible at both sites.",
     "reliable"),
    ("Ixobrychus minutus", "Little Bittern",
     "Low monotonous 'wuh' booms from reedbeds at dawn/dusk. Plausible at the "
     "olifants margins.",
     "reliable"),
    ("Ploceus ocularis", "Spectacled Weaver",
     "Descending 'tee-tee-tee-tee' trill from riverine and thicket. Reliable, "
     "though conf 0.65 here is mid-range.",
     "reliable"),
    ("Tyto alba", "Barn Owl",
     "Hissing shriek at night. Max conf only 0.52 across 8 rows — could easily be "
     "wind / mechanical noise / brake squeals from camp roads. Audit before trusting.",
     None),
    ("Dryoscopus cubla", "Black-backed Puffback",
     "Loud wing-snaps + clear whistled phrases. Common in canopy of woodland.",
     "reliable"),
    ("Riparia riparia", "Bank Swallow",
     "Same species as Eurasian Sand Martin (Palaearctic migrant). BirdNET often "
     "confuses it with Plain Martin (R. paludicola) — note that Plain Martin is "
     "the resident species over our cameras and is also being detected. Treat "
     "these as likely Plain Martin unless audited.",
     "suspect"),
    ("Apalis ruddi", "Rudd's Apalis",
     "Range-restricted to coastal sand-forest — Tembe is one of the very few "
     "places to find it in South Africa! Soft trilling song. If real, this is the "
     "kind of detection worth banking; confirm by ear before excitement.",
     "rare"),
    ("Buphagus erythrorynchus", "Red-billed Oxpecker",
     "Chittering rattles, almost always while clinging to large game. Max conf "
     "0.54 — plausible but worth confirming.",
     None),
    ("Charadrius hiaticula", "Common Ringed Plover",
     "Palaearctic migrant; soft 'too-li' calls. Plausible at olifants in summer; "
     "low max conf so audit before trusting volume.",
     None),
    ("Emberiza flaviventris", "Golden-breasted Bunting",
     "Cheerful 'weecher-weecher-weecher' song from open woodland. Plausible; "
     "mid-range conf.",
     None),
    ("Numenius phaeopus", "Whimbrel",
     "Palaearctic migrant; rapid 'huhuhuhuhu' titter — more typically coastal but "
     "does occur inland on passage. Worth confirming.",
     None),
    ("Crithagra mozambica", "Yellow-fronted Canary",
     "Cheerful melodic song from open lowveld. Plausible; conf 0.61 mid-range.",
     None),
    ("Emberiza capensis", "Cape Bunting",
     "Rocky hillside species typical of fynbos and karoo edges, atypical for "
     "Kruger lowveld and Tembe coastal forest. Likely BirdNET confusion with "
     "another bunting.",
     "suspect"),
    ("Himantopus himantopus", "Black-winged Stilt",
     "Sharp 'yip-yip' calls from shallow water. Plausible at olifants pools; low "
     "max conf so treat as auditioning candidate.",
     None),
    ("Sylvietta rufescens", "Cape Crombec",
     "Soft 'tit-tit-tit' warble from acacia and broadleaved scrub. Reliable in "
     "lowveld bushveld.",
     "reliable"),
    ("Dicrurus adsimilis", "Fork-tailed Drongo",
     "Varied repertoire including harsh chatter and excellent mimicry. Common "
     "everywhere; reliable when audited.",
     "reliable"),
    ("Ortygornis sephaena", "Crested Francolin",
     "Cackling 'beer-beer-beer' duet at dawn. Plausible; low max conf so audit.",
     None),
    ("Recurvirostra avosetta", "Pied Avocet",
     "Clear 'klute' call from saltpans and dams. Plausible at olifants pools "
     "during low water; reliable when conf ≥0.75.",
     "reliable"),
    ("Telophorus sulfureopectus", "Sulphur-breasted Bushshrike",
     "Now usually Chlorophoneus. Whistled 'weeoo-weeoo' phrases from mid-canopy. "
     "Lowveld; reliable in habitat.",
     "reliable"),
    ("Campephaga flava", "Black Cuckooshrike",
     "Soft thin 'trrr' whistles. Quiet bird; mid conf so audit.",
     None),
    ("Columba livia", "Rock Pigeon",
     "Feral street pigeon — unusual in deep wilderness but possible near camps "
     "and infrastructure.",
     None),
    ("Cuculus gularis", "African Cuckoo",
     "Two-note 'hoop-hoop' similar to Eurasian Cuckoo but slower. Summer migrant. "
     "Plausible; low conf so audit.",
     None),
    ("Estrilda astrild", "Common Waxbill",
     "Tinkling flock contact calls. Reliable around grass + water margins.",
     "reliable"),
    ("Lybius torquatus", "Black-collared Barbet",
     "Loud antiphonal 'too-puddly too-puddly' duets. Reliable in mixed woodland.",
     "reliable"),
    ("Muscicapa striata", "Spotted Flycatcher",
     "Palaearctic migrant; quiet 'tzee' calls. Plausible in summer; low conf "
     "means audit.",
     None),
    ("Nectarinia famosa", "Malachite Sunbird",
     "Fynbos / montane grassland species — far from typical Kruger lowveld. "
     "BirdNET likely confusing it with another sunbird (locally Scarlet-chested "
     "or Marico).",
     "suspect"),
    ("Phoeniculus purpureus", "Green Woodhoopoe",
     "Cackling group laughter from broadleaved canopy. Reliable in habitat.",
     "reliable"),
    ("Thalasseus sandvicensis", "Sandwich Tern",
     "Coastal seabird; very implausible inland Kruger. Treat as a BirdNET "
     "misfire on a similar 'kirrik' call.",
     "suspect"),
    ("Anthus lineiventris", "Striped Pipit",
     "Rocky outcrops with bushveld. Possible but not typical at our cameras; low "
     "conf so audit.",
     None),
    ("Apaloderma narina", "Narina Trogon",
     "Sand-forest interior species — Tembe is one of the strongholds. Soft "
     "hooting 'hoot-hoot-hoot'. Low max conf; would be a quality find if real, "
     "worth careful audit.",
     "rare"),
    ("Apus apus", "Common Swift",
     "Palaearctic migrant; high-pitched aerial screams. Plausible in summer; "
     "swift IDs from audio are notoriously hard.",
     None),
    ("Bycanistes bucinator", "Trumpeter Hornbill",
     "Nasal mournful wails — like a crying baby — from coastal and riverine "
     "forest. Olifants gallery forest fits; max 0.84 plausible.",
     "reliable"),
    ("Camaroptera brachyura", "Green-backed Camaroptera",
     "'Bleating' sheep-like calls from dense understorey. Common; low conf here "
     "means audit.",
     None),
    ("Cecropis abyssinica", "Lesser Striped Swallow",
     "High-pitched twittering in flight. Plausible; mid conf so audit.",
     None),
    ("Chlorocichla flaviventris", "Yellow-bellied Greenbul",
     "Coastal forest and dense bushveld. Plausible at Tembe; low conf so audit.",
     None),
    ("Cisticola natalensis", "Croaking Cisticola",
     "Distinctive croaking song from rank grassland. Plausible; low conf.",
     None),
    ("Cuculus solitarius", "Red-chested Cuckoo",
     "'Piet-my-vrou' three-note whistle — one of southern Africa's most famous "
     "summer calls. Surprisingly low conf here — worth checking the source date "
     "(absent outside Oct–Mar).",
     None),
    ("Cyanomitra olivacea", "Olive Sunbird",
     "Coastal and montane forest. Possible at Tembe but low conf — audit.",
     None),
    ("Delichon urbicum", "Common House-Martin",
     "Palaearctic migrant; twittering aerial calls. Plausible in summer; low conf.",
     None),
    ("Falco naumanni", "Lesser Kestrel",
     "Migrant in summer roosts; chittering calls. Possible; low conf so audit.",
     None),
    ("Fraseria caerulescens", "Ashy Flycatcher",
     "Quiet thin whistle from sand-forest and riverine canopy. Plausible at "
     "Tembe; max conf 0.99 suggests a real hit on a clean recording.",
     "reliable"),
    ("Indicator variegatus", "Scaly-throated Honeyguide",
     "Sustained 'fweeeeee' trill from canopy. Plausible; mid conf.",
     None),
    ("Merops persicus", "Blue-cheeked Bee-eater",
     "Palaearctic migrant; trilled calls. Plausible at olifants in summer.",
     None),
    ("Nycticorax nycticorax", "Black-crowned Night-Heron",
     "Sharp 'wak!' call in flight at night. Plausible at olifants; low conf so "
     "audit.",
     None),
    ("Strix woodfordii", "African Wood-Owl",
     "Hooting duet from forest. Tembe and gallery forest are possible. Very low "
     "max conf (0.33) — would be a nice find but needs auditing.",
     "rare"),
    ("Trachyphonus vaillantii", "Crested Barbet",
     "Sustained 'tractor' trill — very loud and prolonged. Reliable in habitat.",
     "reliable"),
    ("Tringa nebularia", "Common Greenshank",
     "Ringing 'tew-tew-tew' triplets. Palaearctic migrant on freshwater margins. "
     "Plausible in summer.",
     None),
    ("Turnix sylvaticus", "Small Buttonquail",
     "Cryptic ground species; female low 'boom-boom'. Very rarely heard, even "
     "rarer detected on audio. Worth careful auditing if real.",
     "rare"),
    ("Turtur tympanistria", "Tambourine Dove",
     "Descending 'duh-duh-duh' cooing that slows and fades. Forest understorey. "
     "Plausible at Tembe; low conf.",
     None),
    ("Zosterops pallidus", "Orange River White-eye",
     "Karoo / Northern Cape species — wrong biome for either camera. Likely "
     "confused with Cape White-eye (Z. virens) or African Yellow White-eye.",
     "suspect"),
    ("Arenaria interpres", "Ruddy Turnstone",
     "Coastal migrant; rare inland. Strongly suspect at olifants.",
     "suspect"),
    ("Chalcomitra senegalensis", "Scarlet-chested Sunbird",
     "Lowveld common; loud 'cheeup-chup'. Single low-conf hit — audit.",
     None),
    ("Chloropicus fuscescens", "Cardinal Woodpecker",
     "Small woodpecker; 'krek-krek-krek' rattles and weak drumming. Plausible "
     "in mixed woodland.",
     None),
    ("Ciconia nigra", "Black Stork",
     "Mostly silent stork; calls rarely captured. Possible at olifants pools. "
     "Very low conf — would be unusual.",
     "rare"),
    ("Cisticola brachypterus", "Siffling Cisticola",
     "Thin 'siff-siff' song. Plausible but low conf.",
     None),
    ("Coracias garrulus", "European Roller",
     "Palaearctic migrant; harsh corvid-like calls. Plausible Oct–Mar.",
     None),
    ("Corvus albicollis", "White-necked Raven",
     "Cliff / mountain species — wrong habitat for both cameras. Likely confused "
     "with Pied Crow or Cape Crow.",
     "suspect"),
    ("Cossypha heuglini", "White-browed Robin-Chat",
     "Rich whistled crescendo at dawn — wonderful song. Plausible; low conf so "
     "audit.",
     None),
    ("Cossypha natalensis", "Red-capped Robin-Chat",
     "Mimicking song similar to White-browed. Forest understorey. Possible at "
     "Tembe; low conf.",
     None),
    ("Coturnix coturnix", "Common Quail",
     "'Wet-my-lips' triplet whistle from grassland. Plausible but low conf.",
     None),
    ("Cuculus clamosus", "Black Cuckoo",
     "Mournful descending three-note 'I'm so sick'. Summer migrant; low conf.",
     None),
    ("Dendrocygna viduata", "White-faced Whistling-Duck",
     "Three-note whistled 'whee-wheeoo' in flight. Plausible at olifants; low "
     "conf.",
     None),
    ("Egretta garzetta", "Little Egret",
     "Largely silent; vocal detection unusual. Single low-conf hit — probably "
     "FP, audit.",
     None),
    ("Eurillas virens", "Little Greenbul",
     "Central / East African range — not normally southern African. Likely "
     "BirdNET confusion with another greenbul.",
     "suspect"),
    ("Falco amurensis", "Amur Falcon",
     "Migrant in summer; chittering calls from roosts. Plausible; very low conf.",
     None),
    ("Gallinula chloropus", "Eurasian Moorhen",
     "Loud 'kurrk' from reed margins. Plausible at olifants; low conf.",
     None),
    ("Iduna natalensis", "African Yellow-Warbler",
     "Reedbed warbler; song similar to Eurasian Reed Warbler. Plausible; low conf.",
     None),
    ("Indicator indicator", "Greater Honeyguide",
     "Repetitive 'VIC-tor, VIC-tor' from open woodland. Plausible; very low conf.",
     None),
    ("Lophoceros nasutus", "African Gray Hornbill",
     "Plaintive piping 'pee-pee-pee'. Reliable in habitat when audited.",
     None),
    ("Mirafra rufocinnamomea", "Flappet Lark",
     "Wing-clap display flight, often without vocalisation. Plausible; very low "
     "conf.",
     None),
    ("Monticola rupestris", "Cape Rock-Thrush",
     "Rocky cliff species — wrong habitat for both cameras. Suspect.",
     "suspect"),
    ("Motacilla capensis", "Cape Wagtail",
     "Resident wagtail of gardens, water margins; BirdNET sometimes confuses "
     "with African Pied Wagtail (which we also detect 170+ times). Likely the "
     "Pied Wagtail mis-labelled.",
     "suspect"),
    ("Oriolus oriolus", "Eurasian Golden Oriole",
     "Palaearctic migrant; flutey whistles. Plausible at olifants in summer.",
     None),
    ("Phoenicopterus roseus", "Greater Flamingo",
     "Saline lake species — unusual at riverine olifants. Suspect.",
     "suspect"),
    ("Phyllastrephus cabanisi", "Cabanis's Greenbul",
     "Coastal and mid-altitude forest, KZN. Tembe sand-forest is plausible; "
     "low conf.",
     None),
    ("Pluvialis squatarola", "Black-bellied Plover",
     "Coastal migrant — rare inland. Suspect at olifants.",
     "suspect"),
    ("Pogoniulus pusillus", "Red-fronted Tinkerbird",
     "Monotonous metronomic 'tonk-tonk-tonk' from canopy. Plausible; mid conf.",
     None),
    ("Stephanoaetus coronatus", "Crowned Eagle",
     "Whistled wailing display calls high over forest canopy. Plausible at "
     "olifants gallery forest; low conf.",
     None),
    ("Sylvia borin", "Garden Warbler",
     "Palaearctic migrant; rich warbling song. Plausible in summer; very low "
     "conf so audit.",
     None),
    ("Tchagra senegalus", "Black-crowned Tchagra",
     "Descending whistled 'whee-cher-cher-cher' song. Common in thornveld; "
     "max conf 0.86 makes this reliable.",
     "reliable"),
    ("Tringa ochropus", "Green Sandpiper",
     "Palaearctic migrant; high 'tlui-tlui' calls. Plausible at olifants; very "
     "low conf.",
     None),
    ("Upupa epops", "Eurasian Hoopoe",
     "Distinctive 'hoop-hoop-hoop' triplet. Resident African subspecies plus "
     "Palaearctic migrants in summer. Plausible; low conf so audit.",
     None),
]


def main() -> int:
    db = Database(AppConfig().db_url)
    added = updated = skipped = 0
    for sci, common, note, tag in NOTES:
        existing = db.get_species_note(sci)
        if existing is not None:
            print(f"  · skipping (already noted) {common}")
            skipped += 1
            continue
        db.set_species_note(sci, common_name=common, note=note, tag=tag)
        print(f"  + {tag or 'note':<8} {common}")
        added += 1
    print(f"\nseeded {added} new species notes; {skipped} already present, {updated} updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
