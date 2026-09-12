"""Preview the 定投调节 table with the reporter's real 30-row payload."""
import tests.check_dca_endpoints as H
from flask import jsonify, redirect, session

app = H.create_app()
H.dh.peer_percentiles = lambda t, recs=None: {"percentile": 62.5, "group": "Tech"}
H.dh.eps_drift = lambda t: {"drift": -0.031}
H._seed("MSFT")
B = {80: "very_cheap", 60: "cheap", 40: "fair", 20: "expensive", 0: "very_expensive"}
def band(v):
    for lo, k in sorted(B.items(), reverse=True):
        if v >= lo: return k
    return "very_expensive"
RAW = [
 ("NVDA","NVIDIA Corporation",9.79,9,187279,60,1.10,1.08,0.60,0.71,711),
 ("MSFT","Microsoft Corporation",9.71,9,185741,53,1.03,1.08,0.60,0.67,670),
 ("AMZN","Amazon.com, Inc.",6.54,5,125102,40,0.90,1.08,0.85,0.82,824),
 ("AAPL","Apple Inc.",5.01,7,95749,18,0.68,0.90,0.85,0.52,519),
 ("META","Meta Platforms, Inc.",4.41,4,84251,71,1.21,0.90,0.85,0.92,923),
 ("TSM","Taiwan Semiconductor Manufacturing Company Limited",4.20,2,80326,49,0.99,1.08,0.85,0.91,912),
 ("UNH","UnitedHealth Group Incorporated",3.57,1,68236,23,0.73,1.08,1.00,0.79,792),
 ("SPAXX","Fidelity Government Money Market Fund",3.45,1,66069,None,None,None,None,None,None),
 ("JPM","JP Morgan Chase & Co.",3.14,4,60095,20,0.70,1.08,1.00,0.76,756),
 ("CRM","Salesforce, Inc.",2.51,2,48076,76,1.26,1.08,1.00,1.37,1365),
 ("NFLX","Netflix, Inc.",2.43,1,46440,77,1.27,1.00,1.00,1.27,1271),
 ("GOOG","Alphabet Inc.",2.12,5,40549,50,1.00,1.00,1.00,1.00,1003),
 ("AVGO","Broadcom Inc.",1.95,8,37293,51,1.01,1.00,1.00,1.01,1008),
 ("MU","Micron Technology, Inc.",1.48,4,28370,51,1.01,1.08,1.00,1.09,1093),
 ("TSLA","Tesla, Inc.",1.41,2,27050,16,0.66,0.70,1.00,0.46,459),
 ("AMD","Advanced Micro Devices, Inc.",1.13,4,21567,44,0.94,1.08,1.00,1.01,1013),
 ("PLTR","Palantir Technologies Inc.",0.87,3,16680,47,0.97,1.08,1.00,1.04,1045),
 ("NLY","Annaly Capital Management Inc.",0.70,1,13320,None,None,None,None,None,None),
 ("CSCO","Cisco Systems, Inc.",0.68,3,13031,8,0.58,1.00,1.00,0.58,583),
 ("LRCX","Lam Research Corporation",0.65,3,12349,12,0.62,1.08,1.00,0.67,669),
 ("INTC","Intel Corporation",0.59,2,11285,None,None,None,None,None,None),
 ("GOOGL","Alphabet Inc.",0.44,4,8444,46,0.96,1.08,1.00,1.04,1039),
 ("NEM","Newmont Corporation",0.37,1,7078,77,1.27,0.70,1.00,0.89,892),
 ("AEM.TO","AGNICO EAGLE MINES LIMITED",0.37,1,7037,69,1.19,1.00,1.00,1.19,1195),
 ("PANW","Palo Alto Networks, Inc.",0.27,1,5129,13,0.63,1.00,1.00,0.63,629),
 ("ABX.TO","BARRICK MINING CORPORATION",0.26,1,4901,73,1.23,1.00,1.00,1.23,1227),
 ("000660.KQ","SK hynix Inc",0.24,1,4513,44,0.94,1.00,1.00,0.94,941),
 ("005930.KQ","Samsung Electronics Co Ltd",0.22,1,4203,13,0.63,1.00,1.00,0.63,631),
 ("CRWD","CrowdStrike Holdings, Inc.",0.20,1,3892,3,0.53,0.90,1.00,0.48,479),
 ("WPM.TO","WHEATON PRECIOUS METALS CORP",0.20,1,3797,41,0.91,1.00,1.00,0.91,909),
]
ST = {"SPAXX":"no_statements","NLY":"no_score","INTC":"no_score"}
ROWS=[]
for (t,n,pc,ro,val,V,mv,me,mp,mu,am) in RAW:
    d = pc if ro==1 else round(pc*0.35,3)
    ROWS.append(dict(ticker=t,name=n,value=float(val),position_pct=pc,direct_pct=d,
        indirect_pct=round(pc-d,3),route_count=ro,status=ST.get(t,"ok"),
        model="compounder",V=V,band=None if V is None else band(V),
        m_valuation=mv,m_earnings=me,m_portfolio=mp,multiplier=mu,
        amount=None if am is None else float(am),capped=False))
PAYLOAD={"base_dca":1000.0,"rows":ROWS,"scored":27,"ranked":30,"exposure_count":78,
 "not_ranked":48,"not_ranked_value":94909.0,"scored_share_pct":87.0,"coverage_pct":74.0,
 "weighted_multiplier":0.82,"weighted_v":47.1,"flat_total":30000.0,"total":24600.0,
 "max_multiplier":1.5,"generated_at":0}
app.view_functions["main.api_dca_portfolio"] = lambda: jsonify(PAYLOAD)
@app.route("/__dev_login")
def _dev():
    session["user_email"]="preview@example.com"
    return redirect("/assets?lang=zh")
app.run(port=5099, debug=False, use_reloader=False)
