"""Starting mapping from merchant name to spending category.

Each entry is (category, regular expression) matched case-insensitively against
the raw description. Order matters: later rules win, so a specific rule can
follow a broad one. Seeded once with `rules.py seed`; after that it is just
rows in the `rule` table and you edit them like any other.

Categories are deliberately narrow. "Eating out" hides the difference between a
$4 coffee and a $60 dinner, and that difference is the point.
"""

SEED = [
    # Broad fallbacks come first. Rules run in this order and the last match
    # wins, so anything specific below can still override them.
    ("Paying people", r"^PAYMENT TO |^ANZ (MOBILE|INTERNET) BANKING PAYMENT \d+ TO "),

    # ---- food and drink ----
    ("Groceries", r"WOOLWORTHS|WW METRO|\bCOLES\b|ALDI STORES|\bIGA\b|FOODWORKS|CT MART"),
    ("Convenience store", r"7-ELEVEN|CAMPUSGENERAL STORE|CAMPUS GENERAL"),
    ("Fast food", r"\bKFC\b|MCDONALDS|HUNGRY JACKS|SUBWAY|DOMINOS|GUZMAN Y GOMEZ|ZAMBRERO"
                  r"|SCHMUCKS BAGELS|MOFO BURGERS|SAMSAM CHICKEN"),
    ("Restaurants", r"DON ?TOJO|EAT NGO[CN]|SUSHI N POKE|SUSHI HUB|GNOCCHI PROJECT|THAILANDER"
                    r"|KAN EANG|UNIVERSAL RESTAURAN|ALLEYWAY KITCHEN|UDON YASAN|IPPUDO"
                    r"|DODEE PAIDANG|GYOZA SAN|KHAO ?SOI|SICHUAN STREET|MALAYSIAN LAKSA"
                    r"|DRAGON HOT POT|RICE WORKSHOP|GU SAM BBQ|VPR ASIAN STREET|MENYARAMEN"
                    r"|HOI AN TOWN|YARRA BOTANICA|CEYLON EXPRESS|KHAOSAN LANE|NANA'S GREEN TEA"
                    r"|MELBOURNE CENTRAL NOMI|ELJANNAH|NICOSIA TURKISH|KATA KITA"),
    ("Cafes", r"STARBUCKS|STANDING ROOM COFFEE|CAFECOMMERCIO|CAFE COMMERCIO|AMBER CAFE"
              r"|HARERUYA PANTRY|COSMOS - CANTEEN|BOOST JUICE|BOOST MC"),
    ("Bubble tea and dessert", r"GONG ?CHA|SHARETEA|TEA WHITE|TOPTEA|HEYTEA|YO-CHI|PICCOLINA"
                               r"|HOMM DESSERT|DESSERT STORY|TRIO PASTRY|KC CHA"),
    ("Alcohol and bottle shops", r"\bBWS\b|DAN MURPHY|LIQUORLAND|FIRST CHOICE LIQUOR"
                                 r"|VINTAGE CELLARS|BOTTLE ?O\b"),
    ("Bars and pubs", r"QUEENSBURY HOTEL|BOARDIES BAR|LA LA LAND|BORSCH VODKA|MAX ON HARDWARE"
                      r"|CARETAKER'S COTTAGE|MAD SPIRIT"),
    ("Vending machines", r"ALTAVEND|BD ?VENDING|B D VENDING|AUSTRALIAN VENDING"),

    # ---- getting around ----
    ("Public transport", r"\bMYKI\b|DEPARTMENT OF TRANSPORT|\bDOT MYKI|TRANSPORTFORNSW"),
    # UBER *EATS is food, not a ride. \bOLA\b, not "OLA ", which also matched
    # the middle of "TST-HOLA MEXICO".
    ("Taxis and rideshare", r"UBER ?\*(?!EATS)|DIDI|\bOLA\b|SHEBAH"),
    ("Food delivery", r"UBER ?\*EATS|DOORDASH|MENULOG|DELIVEROO|HUNGRYPANDA"),
    ("Parking", r"ONSTREET PARKING|WILSON PARKING|SECURE PARKING|CARE PARK"),

    # ---- home ----
    # Rent is rent. The UniLodge card taps are small laundry and amenity
    # charges: 97 of them sat in Rent and dragged its average far below any real rent.
    ("Rent", r"ACQUIRE REAL ESTATE"),
    ("Laundry and amenities", r"UNILODGE|RESIDENT APP"),
    ("Phone and internet", r"VODAFONE|BOOST PREPAID|TELSTRA|OPTUS|AMAYSIM|BELONG"),

    # ---- shopping ----
    ("Clothing", r"UNIQLO|H&M|COTTON ON|MYER|DAVID JONES|GLUE STORE|CULTURE KINGS"),
    ("Electronics", r"JB HI ?FI|OFFICEWORKS|APPLE STORE|MWAVE|SCORPTEC|CENTRE ?COM"),
    # "LS HOUSE OF CARDS ESPR PARKVILLE" is an espresso bar, not a homewares shop.
    ("Homewares", r"\bIKEA\b|\bKMART\b|BIG W|TARGET AUS|DAISO"),
    ("Online shopping", r"\bETSY\b|EBAY|AMAZON MKTPLACE|ALIEXPRESS|TEMU|WISH GIFT CARD"),

    # ---- health and sport ----
    ("Pharmacy and health", r"CHEMIST WAREHOUSE|\bCWH\b|PRICELINE|HEALTHSMART|\bBUPA\b|TERRY WHITE"),
    ("Sports", r"BADMINTON|\bMMBA\b|TENNIS|BASKETBALL|SWIM|CLIMBING|BOULDER"),
    ("Gym and fitness", r"GOODLIFE|ANYTIME FITNESS|FITNESS FIRST|F45|GYM\b"),
    ("Personal care", r"BARBER|HAIRDRESS|\bSALON\b|NAILS"),

    # ---- study ----
    # Named in full. A bare "MONASH" matched "GUZMAN Y GOMEZ MONASH CLAYTON",
    # which is a burrito.
    ("Education", r"UNIVERSITY OF MELBOURN|UNI OF MELBOURNE|MELBOURNE UNIVERSITY THE"
                  r"|\bRMIT\b|MONASH (UNIVERSITY|COLLEGE)|DEAKIN UNIVERSITY"),

    # ---- subscriptions and software ----
    ("Software and subscriptions", r"SPOTIFY|NETFLIX|APPLE\.COM/BILL|AUDIBLE|OPENAI|GOOGLE ONE"
                                   r"|ANTHROPIC|CURSOR|FEEDLY|NAMECHEAP|NAME-CHEAP|FOLK\.APP"
                                   r"|WISPR|MIDJOURNEY|GITHUB|NOTION|DROPBOX|ADOBE|PADDLE\.NET"),

    # ---- going out ----
    ("Entertainment", r"HOYTS|CINEMA NOVA|VILLAGE CINEMAS|EVENT CINEMAS|PALACE CINEMA"
                      r"|MAHONS AMUSEMENTS|TICKETEK|TICKETMASTER|MOSHTIX|ICUE LOUNGE"),
    ("Games", r"FORTRESS MELBOURNE|STEAM ?GAMES|NINTENDO|PLAYSTATION|XBOX"),

    # ---- money out that is not shopping ----
    ("Government and visas", r"VFS SERVICES|DEPT OF HOME AFFAIRS|AUSTRALIAN TAXATION|VICROADS"),
    ("Donations", r"EVERY\.ORG|FARMKIND|RED CROSS|OXFAM|UNICEF"),
    # Cash out of a machine is spending, not a fee. This sits before Bank fees
    # so the separate "PLUS ATM TRANSACTION FEE" line is relabelled by it below.
    ("Cash withdrawals", r"\bATM\b|WITHDRAWAL AT "),
    # Only wording that names an actual fee, and only where the fee IS the
    # transaction. "INC O/S FEE $0.91" appears inside every overseas purchase
    # description, so matching it labelled 123 real purchases as bank fees.
    # "INCL OVERSEAS TRANSACTION FEE $5.67" is the same trap on the other side:
    # ANZ prints it inside a whole overseas ATM withdrawal, so matching it moved
    # whole withdrawals out of Cash withdrawals and into fees.
    ("Bank fees", r"ATM TRANSACTION FEE|ACCOUNT SERVICE FEE"
                  r"|MONTHLY ACCOUNT FEE|DISHONOUR FEE"),
    # Withheld from interest income. It is a tax, not a bank's charge.
    ("Tax", r"RESIDENT WITHHOLD TAX|AUSTRALIAN TAXATION|TRANSFER FROM ATO"),
]
