"""
bus_routes.py - surveyed road geometry for the Delhi Transit Radar routes.

The coordinate lists below were extracted by hand, cut by cut, for BOTH
carriageways of all three routes. They are the ground truth this app uses to

  1. decide which way a bus is actually travelling (map-matched progression,
     not a guess from the feed's route_id),
  2. point every bus icon along the real road instead of along a noisy
     GPS-derived bearing, and
  3. draw the route itself on the map.

Keeping the data here, in its own module, means new survey points can be pasted
in without touching application code. Nothing in this file imports Streamlit, so
it can be unit-tested on its own.

MEASURED PROPERTIES OF THIS DATA (verified, do not assume otherwise):
  * The two carriageways of a route sit a MEDIAN OF 12-14 m apart, which is
    smaller than the GPS scatter on these buses. Deciding direction by "which
    polyline is nearer" is therefore impossible; progression along the path is
    the only reliable signal. See match_direction().
  * Point spacing is wildly uneven: 6.5 m at tight cuts, 3522 m on the straight
    Dwarka diagonal. Both the smoother and the projector are written to cope.
  * The 3522 m gap is NOT missing data. The opposite carriageway's intermediate
    points lie within ~10-14 m of the straight chord, i.e. the road really is
    straight there.
"""

from __future__ import annotations

import math
from typing import Sequence

# --- DATA: surveyed road geometry, one entry per GTFS route_id -------------
# Hand-extracted cut-to-cut coordinates, one list per travel direction.
# Validated on import: see _validate() at the bottom of this module.
ROUTE_PATHS: dict[str, tuple[tuple[float, float], ...]] = {
    # D-9919 Towards Dwarka Mor  -  230 surveyed points, 22.51 km
    "3752": (
        (28.5209750, 77.0818790), (28.5211630, 77.0819620),
        (28.5213090, 77.0820020), (28.5216290, 77.0821410),
        (28.5218860, 77.0822630), (28.5228200, 77.0826660),
        (28.5230800, 77.0828200), (28.5243780, 77.0836540),
        (28.5251270, 77.0841980), (28.5273740, 77.0857270),
        (28.5280970, 77.0861700), (28.5285340, 77.0864580),
        (28.5290310, 77.0867600), (28.5297937, 77.0872185),
        (28.5298871, 77.0870616), (28.5302073, 77.0866918),
        (28.5305290, 77.0863646), (28.5309057, 77.0859944),
        (28.5311985, 77.0856065), (28.5315691, 77.0850171),
        (28.5316115, 77.0849376), (28.5316477, 77.0848283),
        (28.5317013, 77.0845987), (28.5317390, 77.0843549),
        (28.5317540, 77.0839982), (28.5317431, 77.0832689),
        (28.5317481, 77.0830634), (28.5317293, 77.0825374),
        (28.5316518, 77.0818839), (28.5314271, 77.0810233),
        (28.5311738, 77.0802836), (28.5310070, 77.0799997),
        (28.5307496, 77.0796332), (28.5305811, 77.0794324),
        (28.5302700, 77.0791300), (28.5293348, 77.0783018),
        (28.5282057, 77.0772289), (28.5278019, 77.0768296),
        (28.5276531, 77.0766382), (28.5275921, 77.0760317),
        (28.5276166, 77.0751365), (28.5276593, 77.0745558),
        (28.5278723, 77.0728673), (28.5279642, 77.0724278),
        (28.5284252, 77.0708138), (28.5290559, 77.0688873),
        (28.5294347, 77.0677312), (28.5296303, 77.0672642),
        (28.5304518, 77.0656921), (28.5306801, 77.0652700),
        (28.5316203, 77.0634457), (28.5317788, 77.0631276),
        (28.5319417, 77.0627973), (28.5323797, 77.0619330),
        (28.5329199, 77.0608534), (28.5334645, 77.0597936),
        (28.5337172, 77.0593061), (28.5337844, 77.0591475),
        (28.5338790, 77.0588823), (28.5338845, 77.0587941),
        (28.5338881, 77.0587170), (28.5338860, 77.0586339),
        (28.5338666, 77.0585085), (28.5338280, 77.0583931),
        (28.5335956, 77.0574933), (28.5334816, 77.0570996),
        (28.5332990, 77.0564931), (28.5332177, 77.0562983),
        (28.5332012, 77.0561870), (28.5330027, 77.0554715),
        (28.5328990, 77.0550930), (28.5325932, 77.0537221),
        (28.5325555, 77.0534713), (28.5325446, 77.0533382),
        (28.5325487, 77.0532658), (28.5326024, 77.0529898),
        (28.5326589, 77.0527712), (28.5326984, 77.0526073),
        (28.5327331, 77.0525463), (28.5330044, 77.0516273),
        (28.5330601, 77.0514338), (28.5330913, 77.0513118),
        (28.5335862, 77.0501272), (28.5338660, 77.0494473),
        (28.5338993, 77.0493638), (28.5339058, 77.0492465),
        (28.5340781, 77.0488344), (28.5344474, 77.0479573),
        (28.5345626, 77.0476331), (28.5346795, 77.0472335),
        (28.5350471, 77.0458099), (28.5351355, 77.0455363),
        (28.5351959, 77.0452939), (28.5352630, 77.0448832),
        (28.5353926, 77.0439920), (28.5354533, 77.0436748),
        (28.5359773, 77.0424937), (28.5364595, 77.0415006),
        (28.5368995, 77.0403244), (28.5372550, 77.0394611),
        (28.5373166, 77.0393025), (28.5377310, 77.0379346),
        (28.5378874, 77.0373214), (28.5379455, 77.0369814),
        (28.5380158, 77.0364828), (28.5381198, 77.0356711),
        (28.5382365, 77.0340638), (28.5384527, 77.0327995),
        (28.5387392, 77.0315583), (28.5387855, 77.0314503),
        (28.5388479, 77.0314369), (28.5388665, 77.0315000),
        (28.5388886, 77.0316572), (28.5388073, 77.0321829),
        (28.5386747, 77.0327150), (28.5385201, 77.0335066),
        (28.5384120, 77.0342875), (28.5383077, 77.0349818),
        (28.5382415, 77.0359414), (28.5383195, 77.0362528),
        (28.5384653, 77.0365667), (28.5391542, 77.0370062),
        (28.5412074, 77.0387453), (28.5415880, 77.0390182),
        (28.5427328, 77.0400341), (28.5447736, 77.0418107),
        (28.5459938, 77.0428749), (28.5468355, 77.0435240),
        (28.5471515, 77.0437557), (28.5486220, 77.0450837),
        (28.5522597, 77.0481391), (28.5524026, 77.0482514),
        (28.5541495, 77.0499861), (28.5544231, 77.0502094),
        (28.5546449, 77.0501775), (28.5547727, 77.0502033),
        (28.5548810, 77.0502590), (28.5550362, 77.0503908),
        (28.5551146, 77.0505225), (28.5551549, 77.0506677),
        (28.5551632, 77.0509165), (28.5555763, 77.0513148),
        (28.5572956, 77.0528745), (28.5564245, 77.0540724),
        (28.5547686, 77.0565726), (28.5536621, 77.0582151),
        (28.5564195, 77.0605858), (28.5595466, 77.0632955),
        (28.5604465, 77.0635557), (28.5612275, 77.0635638),
        (28.5618791, 77.0634065), (28.5628135, 77.0629740),
        (28.5631036, 77.0627802), (28.5633483, 77.0623380),
        (28.5651136, 77.0596521), (28.5659846, 77.0583854),
        (28.5674381, 77.0562021), (28.5691168, 77.0536761),
        (28.5700193, 77.0523116), (28.5704138, 77.0518737),
        (28.5727261, 77.0538672), (28.5727935, 77.0539360),
        (28.5752221, 77.0560325), (28.5767322, 77.0573035),
        (28.5771845, 77.0576616), (28.5777174, 77.0581782),
        (28.5787523, 77.0589966), (28.5816232, 77.0614274),
        (28.5818997, 77.0616695), (28.5851208, 77.0644160),
        (28.5863806, 77.0655023), (28.5890343, 77.0677631),
        (28.5907636, 77.0692635), (28.5919285, 77.0703015),
        (28.5923433, 77.0706086), (28.5926772, 77.0708466),
        (28.5933719, 77.0712131), (28.5940184, 77.0715514),
        (28.5942362, 77.0716221), (28.5946448, 77.0715578),
        (28.5947511, 77.0715061), (28.5949622, 77.0712486),
        (28.5951394, 77.0709177), (28.5952459, 77.0706160),
        (28.5953911, 77.0700544), (28.5957022, 77.0692175),
        (28.5960131, 77.0684615), (28.5964985, 77.0671184),
        (28.5971700, 77.0651945), (28.5982901, 77.0635353),
        (28.5988473, 77.0626847), (28.5993439, 77.0620114),
        (28.5998626, 77.0611853), (28.6007195, 77.0598673),
        (28.6009906, 77.0594560), (28.6017253, 77.0583459),
        (28.6023562, 77.0574225), (28.6031863, 77.0561642),
        (28.6037099, 77.0553518), (28.6041167, 77.0547323),
        (28.6043375, 77.0542518), (28.6045267, 77.0538022),
        (28.6047505, 77.0531816), (28.6048305, 77.0529161),
        (28.6050133, 77.0521687), (28.6050807, 77.0518485),
        (28.6051823, 77.0511522), (28.6052361, 77.0505698),
        (28.6052547, 77.0499542), (28.6052391, 77.0494359),
        (28.6051060, 77.0484995), (28.6049842, 77.0478896),
        (28.6046745, 77.0467517), (28.6044181, 77.0460456),
        (28.6041096, 77.0453827), (28.6036460, 77.0445167),
        (28.6033084, 77.0440024), (28.6027968, 77.0433671),
        (28.6024177, 77.0429574), (28.6018990, 77.0424809),
        (28.6017118, 77.0423163), (28.6035698, 77.0395003),
        (28.6049633, 77.0373995), (28.6063285, 77.0355213),
        (28.6081829, 77.0349365), (28.6104078, 77.0342120),
        (28.6115045, 77.0338157), (28.6147671, 77.0327546),
        (28.6157872, 77.0324109), (28.6189735, 77.0314802),
    ),
    # D-9919 Towards Kapashera Border  -  112 surveyed points, 22.32 km
    "3753": (
        (28.6189868, 77.0316264), (28.6130800, 77.0334499),
        (28.6063482, 77.0356480), (28.6061307, 77.0359149),
        (28.6045997, 77.0381529), (28.6021848, 77.0417926),
        (28.6022908, 77.0426026), (28.6033317, 77.0438435),
        (28.6039377, 77.0447883), (28.6044217, 77.0457046),
        (28.6048332, 77.0467869), (28.6051025, 77.0478849),
        (28.6053186, 77.0491831), (28.6053559, 77.0503016),
        (28.6052612, 77.0512655), (28.6050062, 77.0526009),
        (28.6047340, 77.0534927), (28.6044087, 77.0544305),
        (28.6040196, 77.0552037), (28.6035807, 77.0559061),
        (28.6028686, 77.0569129), (28.6001664, 77.0608698),
        (28.5988723, 77.0629331), (28.5988082, 77.0635007),
        (28.5989253, 77.0640710), (28.6001517, 77.0650474),
        (28.6005352, 77.0659472), (28.6014981, 77.0675479),
        (28.6025884, 77.0688564), (28.6031745, 77.0702049),
        (28.5996565, 77.0717656), (28.5982562, 77.0721153),
        (28.5971447, 77.0722125), (28.5964614, 77.0722022),
        (28.5941653, 77.0717321), (28.5925544, 77.0708724),
        (28.5919921, 77.0704711), (28.5863079, 77.0655972),
        (28.5861781, 77.0655074), (28.5817692, 77.0617184),
        (28.5787997, 77.0591653), (28.5770767, 77.0577813),
        (28.5707574, 77.0523417), (28.5701044, 77.0524051),
        (28.5631427, 77.0628258), (28.5629864, 77.0629304),
        (28.5617578, 77.0635416), (28.5611801, 77.0636657),
        (28.5606120, 77.0636640), (28.5601394, 77.0635802),
        (28.5597342, 77.0634608), (28.5593908, 77.0633083),
        (28.5592568, 77.0632080), (28.5510334, 77.0561666),
        (28.5507366, 77.0557699), (28.5503723, 77.0550048),
        (28.5503019, 77.0545643), (28.5506314, 77.0542169),
        (28.5524238, 77.0532161), (28.5532325, 77.0525221),
        (28.5536068, 77.0520796), (28.5539637, 77.0514717),
        (28.5540535, 77.0512021), (28.5540037, 77.0506563),
        (28.5536624, 77.0502818), (28.5508508, 77.0481032),
        (28.5495211, 77.0469086), (28.5484385, 77.0458833),
        (28.5469150, 77.0444467), (28.5464626, 77.0440601),
        (28.5455502, 77.0434780), (28.5434358, 77.0416189),
        (28.5426710, 77.0406916), (28.5391266, 77.0376090),
        (28.5381195, 77.0369884), (28.5374889, 77.0394675),
        (28.5369923, 77.0406983), (28.5368830, 77.0407704),
        (28.5357119, 77.0433801), (28.5355747, 77.0437667),
        (28.5350654, 77.0464288), (28.5344722, 77.0482960),
        (28.5334224, 77.0507213), (28.5330969, 77.0515394),
        (28.5326044, 77.0533090), (28.5326336, 77.0536165),
        (28.5333066, 77.0563875), (28.5339293, 77.0585718),
        (28.5339081, 77.0589775), (28.5331732, 77.0605453),
        (28.5307543, 77.0653317), (28.5295272, 77.0678006),
        (28.5282048, 77.0718461), (28.5278849, 77.0731748),
        (28.5276758, 77.0760622), (28.5277097, 77.0765909),
        (28.5278140, 77.0767364), (28.5304621, 77.0791997),
        (28.5309864, 77.0798431), (28.5314003, 77.0806702),
        (28.5317688, 77.0821485), (28.5317900, 77.0845514),
        (28.5316076, 77.0851509), (28.5308845, 77.0861094),
        (28.5297890, 77.0873657), (28.5253516, 77.0844505),
        (28.5249687, 77.0841957), (28.5248223, 77.0841480),
        (28.5229979, 77.0829276), (28.5223625, 77.0825706),
        (28.5212463, 77.0821461), (28.5208351, 77.0820261),
    ),
    # 718 Towards Uttam Nagar  -  74 surveyed points, 17.85 km
    "2176": (
        (28.5209750, 77.0818790), (28.5211630, 77.0819620),
        (28.5213090, 77.0820020), (28.5216290, 77.0821410),
        (28.5218860, 77.0822630), (28.5228200, 77.0826660),
        (28.5230800, 77.0828200), (28.5243780, 77.0836540),
        (28.5251270, 77.0841980), (28.5273740, 77.0857270),
        (28.5280970, 77.0861700), (28.5285340, 77.0864580),
        (28.5290310, 77.0867600), (28.5297937, 77.0872185),
        (28.5325859, 77.0891014), (28.5354999, 77.0910336),
        (28.5357694, 77.0912147), (28.5379899, 77.0923083),
        (28.5382264, 77.0911801), (28.5384954, 77.0884396),
        (28.5393940, 77.0843033), (28.5408092, 77.0803417),
        (28.5428954, 77.0710414), (28.5443194, 77.0645458),
        (28.5449155, 77.0619179), (28.5457275, 77.0596172),
        (28.5477196, 77.0565052), (28.5486703, 77.0550622),
        (28.5501169, 77.0540433), (28.5504441, 77.0542803),
        (28.5504627, 77.0547158), (28.5508729, 77.0557163),
        (28.5512098, 77.0561666), (28.5594898, 77.0632563),
        (28.5604468, 77.0635463), (28.5612964, 77.0635604),
        (28.5619557, 77.0633810), (28.5630988, 77.0628067),
        (28.5633830, 77.0623920), (28.5685932, 77.0668116),
        (28.5711352, 77.0663694), (28.5715993, 77.0659231),
        (28.5719146, 77.0654128), (28.5798608, 77.0721965),
        (28.5804249, 77.0720476), (28.5840860, 77.0666496),
        (28.5855778, 77.0664361), (28.5862882, 77.0654329),
        (28.5925759, 77.0707813), (28.5941826, 77.0716037),
        (28.5953463, 77.0719162), (28.5965854, 77.0720908),
        (28.5981061, 77.0720248), (28.5991452, 77.0718075),
        (28.6015446, 77.0707762), (28.6031130, 77.0700316),
        (28.6037953, 77.0715899), (28.6054201, 77.0726816),
        (28.6067403, 77.0764276), (28.6079400, 77.0789389),
        (28.6105067, 77.0820526), (28.6109547, 77.0838037),
        (28.6117067, 77.0848407), (28.6121811, 77.0858797),
        (28.6131068, 77.0863977), (28.6132781, 77.0863552),
        (28.6136348, 77.0859713), (28.6139221, 77.0855230),
        (28.6151238, 77.0831141), (28.6154929, 77.0823882),
        (28.6207885, 77.0710589), (28.6216050, 77.0696587),
        (28.6221324, 77.0690314), (28.6254944, 77.0671988),
    ),
    # 718 Towards Kapashera Border  -  74 surveyed points, 17.91 km
    "2179": (
        (28.6254965, 77.0673631), (28.6224361, 77.0689272),
        (28.6219057, 77.0694391), (28.6214434, 77.0701221),
        (28.6199524, 77.0731620), (28.6176694, 77.0779783),
        (28.6162699, 77.0809468), (28.6156480, 77.0824234),
        (28.6154164, 77.0827788), (28.6148248, 77.0838765),
        (28.6133923, 77.0872742), (28.6130718, 77.0871994),
        (28.6129184, 77.0869010), (28.6127574, 77.0866237),
        (28.6120028, 77.0860045), (28.6116063, 77.0849785),
        (28.6108140, 77.0840552), (28.6104546, 77.0837289),
        (28.6105008, 77.0834480), (28.6105894, 77.0831399),
        (28.6105729, 77.0827841), (28.6104334, 77.0821679),
        (28.6079762, 77.0791484), (28.6066046, 77.0763304),
        (28.6053945, 77.0728710), (28.6051467, 77.0725884),
        (28.6037605, 77.0717160), (28.6030503, 77.0702753),
        (28.5994858, 77.0718266), (28.5979533, 77.0721472),
        (28.5963572, 77.0721897), (28.5941073, 77.0717083),
        (28.5929771, 77.0711105), (28.5920692, 77.0705476),
        (28.5818349, 77.0617650), (28.5765700, 77.0695766),
        (28.5724184, 77.0660324), (28.5704512, 77.0666594),
        (28.5695367, 77.0668753), (28.5684855, 77.0669014),
        (28.5633421, 77.0625093), (28.5630885, 77.0628687),
        (28.5614024, 77.0636459), (28.5603075, 77.0636191),
        (28.5593790, 77.0633113), (28.5509822, 77.0561263),
        (28.5507045, 77.0557468), (28.5504385, 77.0551544),
        (28.5501944, 77.0549804), (28.5498003, 77.0550149),
        (28.5486953, 77.0561230), (28.5477988, 77.0574953),
        (28.5466873, 77.0593302), (28.5457134, 77.0607867),
        (28.5450054, 77.0627155), (28.5447462, 77.0634240),
        (28.5432270, 77.0697875), (28.5427944, 77.0722005),
        (28.5415818, 77.0776065), (28.5409483, 77.0804154),
        (28.5396193, 77.0841222), (28.5390385, 77.0865030),
        (28.5384303, 77.0903242), (28.5380465, 77.0925165),
        (28.5359641, 77.0914654), (28.5313107, 77.0883729),
        (28.5297890, 77.0873657), (28.5253516, 77.0844505),
        (28.5249687, 77.0841957), (28.5248223, 77.0841480),
        (28.5229979, 77.0829276), (28.5223625, 77.0825706),
        (28.5212463, 77.0821461), (28.5208351, 77.0820261),
    ),
    # D-068 Towards Dwarka Mor  -  16 surveyed points, 10.54 km
    "2801": (
        (28.5536442, 77.0580756), (28.5571633, 77.0528758),
        (28.5652119, 77.0597269), (28.5634192, 77.0624118),
        (28.5685962, 77.0668233), (28.5713351, 77.0662289),
        (28.5719276, 77.0654386), (28.5771633, 77.0576824),
        (28.5858371, 77.0650202), (28.5861948, 77.0650685),
        (28.5866297, 77.0649246), (28.5868911, 77.0645636),
        (28.6060321, 77.0358183), (28.6062569, 77.0355709),
        (28.6063361, 77.0355266), (28.6189688, 77.0314819),
    ),
    # D-068 Towards Sector 21  -  19 surveyed points, 10.54 km
    "2804": (
        (28.6189868, 77.0316264), (28.6130800, 77.0334499),
        (28.6063482, 77.0356480), (28.6061307, 77.0359149),
        (28.6045997, 77.0381529), (28.6021848, 77.0417926),
        (28.5966522, 77.0500776), (28.5914649, 77.0578624),
        (28.5863067, 77.0655989), (28.5771591, 77.0578634),
        (28.5715536, 77.0661910), (28.5711832, 77.0664562),
        (28.5685293, 77.0669205), (28.5632911, 77.0624664),
        (28.5650314, 77.0597644), (28.5593743, 77.0549871),
        (28.5591944, 77.0549244), (28.5571159, 77.0531437),
        (28.5537146, 77.0581299),
    ),
}

# --- GEOMETRY ENGINE -------------------------------------------------------

# Metres per degree at Delhi's latitude (WGS-84 series, good to <0.01 %).
def _m_per_deg(lat_deg: float) -> tuple[float, float]:
    p = math.radians(lat_deg)
    m_lat = 111132.92 - 559.82 * math.cos(2 * p) + 1.175 * math.cos(4 * p)
    m_lon = 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p)
    return m_lat, m_lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2.0 * r * math.asin(min(1.0, math.sqrt(h)))


class PathIndex:
    """
    A surveyed road polyline prepared for fast, accurate point-to-path queries.

    Every segment carries its OWN local metric frame (metres-per-degree evaluated
    at that segment's own latitude). A single frame for the whole 22 km corridor
    would be ~0.1 % out at the ends - about 6 m - and the two carriageways of
    these roads are only ~12 m apart, so that error alone could flip a lane
    decision. Per-segment frames keep the error under a centimetre.
    """

    __slots__ = ("pts", "n", "_seg", "cum", "length")

    def __init__(self, pts: Sequence[tuple[float, float]]):
        self.pts = tuple((float(a), float(b)) for a, b in pts)
        self.n = len(self.pts)
        self._seg: list[tuple] = []
        self.cum: list[float] = [0.0]
        total = 0.0
        for i in range(self.n - 1):
            (alat, alon), (blat, blon) = self.pts[i], self.pts[i + 1]
            m_lat, m_lon = _m_per_deg((alat + blat) * 0.5)
            bx = (blon - alon) * m_lon
            by = (blat - alat) * m_lat
            ln = math.hypot(bx, by)
            self._seg.append((alat, alon, m_lat, m_lon, bx, by, ln * ln, ln))
            total += ln
            self.cum.append(total)
        self.length = total

    # -- projection ---------------------------------------------------------
    def _project_on(self, i: int, lat: float, lon: float) -> tuple[float, float]:
        """Return (t in 0..1, perpendicular distance m) for segment i."""
        alat, alon, m_lat, m_lon, bx, by, l2, ln = self._seg[i]
        px = (lon - alon) * m_lon
        py = (lat - alat) * m_lat
        if l2 <= 0.0:
            return 0.0, math.hypot(px, py)
        t = (px * bx + py * by) / l2
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        return t, math.hypot(px - t * bx, py - t * by)

    def project(self, lat: float, lon: float,
                s_hint: float | None = None,
                window_m: float = 700.0) -> tuple[float, float, int]:
        """
        Snap (lat, lon) onto the path.

        Returns (s, lateral_m, seg_idx) where s is the distance travelled along
        the path to the snapped point.

        `s_hint` restricts the search to +/- window_m around a previously known
        position. That is essential: these routes double back on themselves (the
        9919 loop near Dwarka Sector 8 passes within ~200 m of its own outbound
        leg), so a global nearest-point search can teleport a bus kilometres
        along the chainage axis and invert the inferred direction.
        """
        lo, hi = 0, self.n - 1
        if s_hint is not None and self.length > 0:
            lo = _bisect_left(self.cum, s_hint - window_m)
            hi = _bisect_right(self.cum, s_hint + window_m)
            lo = max(0, lo - 1)
            hi = min(self.n - 1, hi + 1)
            if hi - lo < 1:
                lo, hi = 0, self.n - 1
        best = (float("inf"), 0.0, 0)
        for i in range(lo, hi):
            t, d = self._project_on(i, lat, lon)
            if d < best[0]:
                best = (d, t, i)
        d, t, i = best
        return self.cum[i] + t * self._seg[i][7], d, i

    # -- sampling -----------------------------------------------------------
    def point_at(self, s: float) -> tuple[float, float]:
        s = min(max(s, 0.0), self.length)
        i = min(max(_bisect_right(self.cum, s) - 1, 0), self.n - 2)
        alat, alon, m_lat, m_lon, bx, by, _l2, ln = self._seg[i]
        t = 0.0 if ln <= 0 else (s - self.cum[i]) / ln
        t = min(max(t, 0.0), 1.0)
        return alat + (by * t) / m_lat, alon + (bx * t) / m_lon

    def tangent_deg(self, s: float, span_m: float = 45.0) -> float:
        """
        Compass bearing of the road at chainage s, measured over a short span so
        a single noisy survey point cannot swing it.
        """
        a = self.point_at(max(0.0, s - span_m * 0.5))
        b = self.point_at(min(self.length, s + span_m * 0.5))
        if a == b:
            i = min(max(_bisect_right(self.cum, s) - 1, 0), self.n - 2)
            a, b = self.pts[i], self.pts[i + 1]
        return bearing_deg(a[0], a[1], b[0], b[1])


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _bisect_left(arr: list[float], v: float) -> int:
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(arr: list[float], v: float) -> int:
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if v < arr[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


# --- SMOOTHING -------------------------------------------------------------

def smooth_path(pts: Sequence[tuple[float, float]],
                max_fillet_m: float = 45.0,
                frac: float = 0.42,
                min_turn_deg: float = 4.0) -> list[list[float]]:
    """
    Round every road cut with a quadratic Bezier fillet, leave straights straight.

    WHY NOT A SPLINE THROUGH THE POINTS: a Catmull-Rom spline was tried first and
    measured. On the densely surveyed 9919 it was fine (max 24 m off the raw
    line), but on the 068 corridor - 16 points across 10.5 km, with roughly 1 km
    between them - it bowed up to 109 m away from the actual road, because a
    spline curves the WHOLE segment, not just the corner. Any interpolating
    spline does this when control points are far apart.

    A fillet curves only the last `r` metres before a vertex and the first `r`
    metres after it, where

        r = min(max_fillet_m, frac * |previous segment|, frac * |next segment|)

    so the deviation from the surveyed line is bounded by r (<= 45 m, and in
    practice far less), a long straight stays perfectly straight, and short
    6 m cuts self-limit to a 2.5 m fillet instead of being over-smoothed.
    Vertices that barely bend at all are passed straight through.

    Returns [lon, lat] pairs, ready for a deck.gl PathLayer.
    """
    p = [(float(a), float(b)) for a, b in pts]
    n = len(p)
    if n < 3:
        return [[q[1], q[0]] for q in p]

    lat0 = sum(q[0] for q in p) / n
    m_lat, m_lon = _m_per_deg(lat0)
    base_lat, base_lon = p[0]
    xy = [((q[1] - base_lon) * m_lon, (q[0] - base_lat) * m_lat) for q in p]

    out: list[tuple[float, float]] = [xy[0]]
    for i in range(1, n - 1):
        ax, ay = xy[i - 1]
        vx, vy = xy[i]
        cx, cy = xy[i + 1]
        d1x, d1y = ax - vx, ay - vy
        d2x, d2y = cx - vx, cy - vy
        l1 = math.hypot(d1x, d1y)
        l2 = math.hypot(d2x, d2y)
        if l1 < 1e-9 or l2 < 1e-9:
            continue
        cosang = (d1x * d2x + d1y * d2y) / (l1 * l2)
        cosang = min(1.0, max(-1.0, cosang))
        turn = 180.0 - math.degrees(math.acos(cosang))   # 0 = straight on
        if turn < min_turn_deg:
            out.append((vx, vy))
            continue
        r = min(max_fillet_m, frac * l1, frac * l2)
        if r < 0.8:
            out.append((vx, vy))
            continue
        px, py = vx + d1x / l1 * r, vy + d1y / l1 * r
        qx, qy = vx + d2x / l2 * r, vy + d2y / l2 * r
        steps = int(min(14, max(3, round(turn / 9.0))))
        out.append((px, py))
        for k in range(1, steps):
            t = k / steps
            u = 1.0 - t
            out.append((u * u * px + 2 * u * t * vx + t * t * qx,
                        u * u * py + 2 * u * t * vy + t * t * qy))
        out.append((qx, qy))
    out.append(xy[-1])

    # drop points closer than 40 cm together (keeps the GPU buffer tidy)
    cleaned = [out[0]]
    for q in out[1:]:
        if math.dist(q, cleaned[-1]) > 0.4:
            cleaned.append(q)
    if cleaned[-1] != out[-1]:
        cleaned.append(out[-1])
    return [[base_lon + x / m_lon, base_lat + y / m_lat] for x, y in cleaned]


def _simplify(pts: list[list[float]], tol_m: float = 3.0) -> list[list[float]]:
    """
    Douglas-Peucker on the DISPLAY path only (never on the data used for
    matching).

    This is a bandwidth fix, not a cosmetic one. Streamlit re-serialises the
    whole deck.gl spec on every auto-refresh, and the route is drawn with three
    stacked PathLayers, so a 621-point smoothed line cost ~66 KB on the wire
    every few seconds. Re-creating layers that big makes the chart component
    rebuild for long enough that taps landing in that window are swallowed -
    measured as only 1 in 6 taps dismissing the detail card while a route was
    displayed. At a 3 m tolerance the line keeps its shape to within 0.73 m
    (far under one screen pixel at any usable zoom) for an eighth of the bytes.
    """
    if len(pts) < 3:
        return pts
    lat0 = pts[0][1]
    m_lat, m_lon = _m_per_deg(lat0)
    xy = [((p[0] - pts[0][0]) * m_lon, (p[1] - pts[0][1]) * m_lat) for p in pts]

    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        dmax, idx = 0.0, i
        for k in range(i + 1, j):
            px, py = xy[k]
            if l2 <= 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > dmax:
                dmax, idx = d, k
        if dmax > tol_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def chevrons_along(idx: "PathIndex", spacing_m: float = 850.0,
                   edge_m: float = 320.0) -> list[dict]:
    """Evenly spaced direction markers with the road's own bearing at each spot."""
    out: list[dict] = []
    if idx.length < edge_m * 2 + spacing_m:
        spacing_m = max(260.0, idx.length / 3.0)
        edge_m = min(edge_m, idx.length / 4.0)
    s = edge_m
    while s <= idx.length - edge_m:
        lat, lon = idx.point_at(s)
        out.append({"lat": lat, "lon": lon, "brg": idx.tangent_deg(s)})
        s += spacing_m
    return out


# --- ROUTE METADATA --------------------------------------------------------
# Which two route_ids form each physical corridor, and which one is used as the
# chainage reference (the denser survey, so projection is most accurate).
DIRECTION_PAIR: dict[str, str] = {
    "3752": "3753", "3753": "3752",      # D-9919
    "2176": "2179", "2179": "2176",      # 718
    "2801": "2804", "2804": "2801",      # D-068
}

# corridor key -> (reference route_id, opposite route_id)
# Progress along the REFERENCE path increasing  => bus is heading the reference way.
CORRIDOR_REF: dict[str, tuple[str, str]] = {
    "D-9919": ("3752", "3753"),   # 230 surveyed points vs 112
    "718":    ("2176", "2179"),   # 74 vs 74, either works
    "D-068":  ("2804", "2801"),   # 19 vs 16
}
ROUTE_OF: dict[str, str] = {
    "3752": "D-9919", "3753": "D-9919",
    "2176": "718", "2179": "718",
    "2801": "D-068", "2804": "D-068",
}

# Built once at import; cheap (525 points total).
PATH_INDEX: dict[str, PathIndex] = {rid: PathIndex(pts) for rid, pts in ROUTE_PATHS.items()}
# Display geometry: corner-filleted, simplified and rounded to ~1 m so the
# per-refresh deck.gl payload stays small. Matching always uses PATH_INDEX,
# which is built from the untouched surveyed points.
SMOOTHED: dict[str, list[list[float]]] = {
    rid: [[round(x, 5), round(y, 5)] for x, y in _simplify(smooth_path(pts))]
    for rid, pts in ROUTE_PATHS.items()
}
CHEVRONS: dict[str, list[dict]] = {rid: chevrons_along(idx) for rid, idx in PATH_INDEX.items()}
TERMINALS: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    rid: (pts[0], pts[-1]) for rid, pts in ROUTE_PATHS.items()
}


# --- SELF-CHECK ------------------------------------------------------------

def _validate() -> list[str]:
    """Structural checks run at import. Returns a list of problems (empty = fine)."""
    problems: list[str] = []
    for rid, pts in ROUTE_PATHS.items():
        if len(pts) < 2:
            problems.append(f"{rid}: fewer than 2 points")
            continue
        for lat, lon in pts:
            if not (28.0 < lat < 29.5 and 76.5 < lon < 78.0):
                problems.append(f"{rid}: point outside the Delhi NCR box: {lat},{lon}")
                break
    for rid, other in DIRECTION_PAIR.items():
        if rid not in ROUTE_PATHS or other not in ROUTE_PATHS:
            problems.append(f"{rid}/{other}: missing from ROUTE_PATHS")
            continue
        a, b = ROUTE_PATHS[rid], ROUTE_PATHS[other]
        # the two lanes must start/end at the same two terminals, reversed
        if haversine_m(a[0][0], a[0][1], b[-1][0], b[-1][1]) > 400:
            problems.append(f"{rid} start is {haversine_m(a[0][0], a[0][1], b[-1][0], b[-1][1]):.0f} m "
                            f"from {other} end - are these really the same corridor?")
    # Geometric fidelity, not point count: the display path is deliberately
    # simplified, so what matters is that it still follows the surveyed road.
    for rid, sm in SMOOTHED.items():
        if len(sm) < 2:
            problems.append(f"{rid}: display path collapsed")
            continue
        disp = PathIndex([(p[1], p[0]) for p in sm])
        worst = max(disp.project(la, lo)[1] for la, lo in ROUTE_PATHS[rid])
        if worst > 30.0:
            problems.append(f"{rid}: drawn line strays {worst:.0f} m from the survey")
    return problems


VALIDATION_PROBLEMS: list[str] = _validate()


# --- DIRECTION MATCHING ----------------------------------------------------
#
# The problem, stated precisely: the two carriageways of these roads are a
# median of 12-14 m apart, and a Delhi bus GPS fix scatters by a similar amount
# or more. So "which polyline is nearer" is a coin toss. What IS unambiguous is
# the SIGN OF PROGRESS: project the bus onto one agreed reference polyline and
# watch whether its chainage goes up or down between fixes. A bus doing 25 km/h
# with 20 s between fixes moves ~140 m along the road - an order of magnitude
# more than the noise - so one or two fixes settle the question.

OFFROUTE_M = 180.0      # beyond this the bus is not on the surveyed corridor
MIN_MOVE_M = 18.0       # straight-line move below this is indistinguishable from noise
NOISE_FLOOR_M = 25.0    # a move this small earns zero weight
FULL_WEIGHT_M = 100.0   # a move this big earns full weight
PROG_DECAY = 0.62       # how fast old evidence is forgotten
PROG_LOCK = 0.80        # |accumulator| above which a direction is declared
PROG_CLAMP = 2.0        # keeps the accumulator responsive when a bus turns round
TANGENT_MAX_LATERAL_M = 70.0   # trust the road's bearing only if genuinely on it
MAX_BUS_MPS = 22.0      # 79 km/h - used to size the re-acquisition window


def new_match_state() -> dict:
    return {"s": None, "prog": 0.0, "dir_rid": None, "lateral": None,
            "on_route": False, "conf": 0.0, "reacquired": 0,
            "lat": None, "lon": None, "heading": None}


def match_direction(route_key: str, lat: float, lon: float,
                    state: dict | None, elapsed_s: float,
                    fallback_rid: str | None = None) -> dict:
    """
    Map-match one GPS fix and update the believed travel direction.

    `state` is the dict returned by the previous call for this same bus (or
    new_match_state() the first time). `elapsed_s` is the time since that fix.
    `fallback_rid` is the direction the feed's own route_id claims - used only
    until movement has proved otherwise.

    Returns the updated state, which additionally carries:
        dir_rid   - route_id of the direction the bus is believed to travel
        heading   - compass bearing of the road there, or None if off-route
        conf      - 0..1, how strongly the progression supports dir_rid
    """
    pair = CORRIDOR_REF.get(route_key)
    if pair is None:
        return dict(new_match_state(), dir_rid=fallback_rid, heading=None)
    ref_rid, opp_rid = pair
    ref = PATH_INDEX[ref_rid]

    st = dict(state) if state else new_match_state()
    prev_s = st.get("s")

    # A long silence means the bus could legitimately be far along the route, so
    # widen the search rather than clamping it to a stale neighbourhood.
    window = 700.0
    if prev_s is not None:
        window = min(5000.0, max(500.0, (elapsed_s or 0.0) * MAX_BUS_MPS * 1.6))

    s_new, lateral, _seg = ref.project(lat, lon, s_hint=prev_s, window_m=window)

    # If the windowed answer is poor but a global search finds the bus clearly
    # somewhere else, the old chainage was stale - re-acquire and throw away the
    # progression rather than inventing a huge bogus delta.
    reacquired = False
    if prev_s is not None and lateral > 120.0:
        g_s, g_lat, _ = ref.project(lat, lon, s_hint=None)
        if g_lat < lateral - 40.0:
            s_new, lateral = g_s, g_lat
            reacquired = True
            st["prog"] = 0.0
            st["reacquired"] = int(st.get("reacquired", 0)) + 1

    on_route = lateral <= OFFROUTE_M
    st["lateral"] = lateral
    st["on_route"] = on_route

    if not on_route:
        # Keep whatever we last believed; a bus on a diversion should not have
        # its direction scrambled by garbage projections.
        st["s"] = s_new
        st["lat"], st["lon"] = lat, lon
        st["heading"] = None
        st["dir_rid"] = st.get("dir_rid") or fallback_rid
        st["conf"] = min(1.0, abs(st.get("prog", 0.0)) / 1.6)
        return st

    # ---- progression vote -------------------------------------------------
    # An earlier version accumulated the raw chainage delta in metres. Measured
    # against a PARKED bus with 14 m GPS scatter, that random-walked across the
    # lock threshold and flipped the reported direction on 62 of 80 fixes -
    # precisely the "sometimes shows the wrong direction" failure. The delta of
    # a stationary bus is pure noise, and noise in metres looks exactly like
    # slow travel in metres.
    #
    # So the vote is normalised instead:
    #     vote   = ds / straight-line distance moved      (+1 dead-on forward,
    #                                                      -1 dead-on backward,
    #                                                      ~0 for noise)
    #     weight = 0 below NOISE_FLOOR_M, ramping to 1 at FULL_WEIGHT_M
    # A parked bus moves ~1.8 sigma between fixes, lands under the noise floor,
    # and contributes nothing at all. A bus doing 25 km/h covers ~140 m between
    # fixes, scores a full-weight +-1, and locks on the very first one.
    prev_lat, prev_lon = st.get("lat"), st.get("lon")
    moved_m = None
    if prev_lat is not None:
        moved_m = haversine_m(prev_lat, prev_lon, lat, lon)

    if prev_s is not None and not reacquired and moved_m is not None and moved_m >= MIN_MOVE_M:
        ds = s_new - prev_s
        vote = ds / moved_m
        vote = max(-1.0, min(1.0, vote))
        weight = (moved_m - NOISE_FLOOR_M) / (FULL_WEIGHT_M - NOISE_FLOOR_M)
        weight = max(0.0, min(1.0, weight))
        if weight > 0.0:
            # The forgetting factor is raised to the power of the evidence, not
            # applied once per fix. That distinction matters a great deal: a bus
            # crawling through traffic, or a feed that republishes every few
            # seconds, only advances ~30 m per fix, which earns a weight near
            # 0.07. With a flat decay such a bus could never accumulate past
            # 0.07/(1-0.62) = 0.18 and would be stuck on the feed's guess for
            # ever, no matter how many consistent fixes it produced. Scaling the
            # decay by the evidence makes the steady state ~2.1 regardless of
            # step size, so consistent small moves still converge - they just
            # take more of them, which is exactly right.
            d = PROG_DECAY ** weight
            prog = st.get("prog", 0.0) * d + vote * weight
            st["prog"] = max(-PROG_CLAMP, min(PROG_CLAMP, prog))

    st["s"] = s_new
    st["lat"], st["lon"] = lat, lon
    prog = st.get("prog", 0.0)
    if prog > PROG_LOCK:
        st["dir_rid"] = ref_rid
    elif prog < -PROG_LOCK:
        st["dir_rid"] = opp_rid
    elif st.get("dir_rid") is None:
        st["dir_rid"] = fallback_rid
    st["conf"] = min(1.0, abs(prog) / 1.6)

    # Heading straight off the road geometry, taken on the bus's OWN carriageway
    # (the two can split at one-way pairs, so the reference path is not always
    # the right one to read a bearing from).
    heading = None
    dir_rid = st.get("dir_rid")
    if dir_rid in PATH_INDEX:
        own = PATH_INDEX[dir_rid]
        s_own, lat_own, _ = own.project(lat, lon, s_hint=None)
        if lat_own <= TANGENT_MAX_LATERAL_M:
            heading = own.tangent_deg(s_own)
            st["s_own"] = s_own
            st["lateral_own"] = lat_own
    st["heading"] = heading
    return st
