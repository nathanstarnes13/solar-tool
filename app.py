"""
Solar & Battery Design Tool - Honest Energy Consulting
Run: streamlit run app.py
Install: pip install streamlit requests fpdf2 pandas
"""
import streamlit as st
import requests
import math
import json
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
# CONFIG — paste your API keys here
# ─────────────────────────────────────────────

GOOGLE_SOLAR_API_KEY = "YOUR_GOOGLE_CLOUD_API_KEY"   # console.cloud.google.com → Solar API
NREL_API_KEY         = "DEMO_KEY"                     # developer.nrel.gov/signup (DEMO_KEY = 50 req/day)
OPENEI_API_KEY       = "DEMO_KEY"                     # openei.org/services/api

# Equipment defaults
PANEL_WATT           = 400       # W per panel
PANEL_SQFT           = 18.5      # sq ft per panel
BATTERY_KWH          = 13.5      # Tesla Powerwall 3 usable kWh
BATTERY_KW           = 11.5      # Tesla Powerwall 3 continuous power (kW)
DOD                  = 0.90      # depth of discharge
RTE                  = 0.95      # round-trip efficiency
INSTALL_COST_PER_W   = 2.15      # $/W installed (AZ market rate)
ITC_PCT              = 0.00      # Federal ITC expired for residential cash purchase 2026
PROD_FACTOR_AZ       = 1750      # kWh/kW/yr baseline for AZ (PVWatts will refine this)


# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────

@dataclass
class GeoResult:
    lat: float
    lon: float
    formatted_address: str

@dataclass
class RoofResult:
    usable_area_sqft: float
    max_array_panels: int
    primary_tilt_deg: float
    primary_azimuth_deg: float
    source: str
    raw: dict = field(default_factory=dict)

@dataclass
class SolarResult:
    ac_annual_kwh: float
    ac_monthly_kwh: list
    peak_sun_hours: float
    prod_factor: float           # kWh/kW/yr
    source: str
    raw: dict = field(default_factory=dict)

@dataclass
class RateResult:
    utility_name: str
    rate_name: str
    rate_id: str
    energy_rate_kwh: float       # flat $/kWh (or blended if TOU)
    is_tou: bool
    has_demand: bool
    net_metering: bool
    source: str
    notes: str = ""
    raw: dict = field(default_factory=dict)

@dataclass
class DesignResult:
    system_kw_dc: float
    panel_count: int
    roof_area_needed_sqft: float
    roof_area_available_sqft: float
    roof_constrained: bool
    battery_count: int
    battery_kwh_total: float
    annual_production_kwh: float
    self_consumption_pct: float
    gross_cost: float
    itc_credit: float
    net_cost: float
    payback_years: float
    annual_savings: float
    year25_savings: float
    assumptions: list


# ─────────────────────────────────────────────
# API 1 — CENSUS BUREAU GEOCODER (no key needed)
# ─────────────────────────────────────────────

def geocode_address(address: str) -> GeoResult:
    """
    Convert a street address to lat/lon using the US Census Bureau Geocoder.
    Free, no API key required, US addresses only.
    Docs: https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html
    """
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            raise ValueError("Address not found. Please check and try again.")
        match = matches[0]
        coords = match["coordinates"]
        return GeoResult(
            lat=float(coords["y"]),
            lon=float(coords["x"]),
            formatted_address=match.get("matchedAddress", address)
        )
    except requests.RequestException as e:
        raise ConnectionError(f"Geocoder API error: {e}")


# ─────────────────────────────────────────────
# API 2 — GOOGLE SOLAR API (roof geometry)
# ─────────────────────────────────────────────

def get_roof_geometry(lat: float, lon: float) -> RoofResult:
    """
    Pull roof geometry from Google Solar API buildingInsights endpoint.
    Returns usable solar panel area, primary tilt, and azimuth.
    Docs: https://developers.google.com/maps/documentation/solar/reference/rest/v1/buildingInsights/findClosest
    Free tier: $200/mo Google Cloud credit covers ~4,000 requests/mo.
    """
    if GOOGLE_SOLAR_API_KEY == "YOUR_GOOGLE_CLOUD_API_KEY":
        # Return a clearly-labelled fallback so the app still runs during development
        return _roof_fallback(lat, lon)

    url = "https://solar.googleapis.com/v1/buildingInsights:findClosest"
    params = {
        "location.latitude": lat,
        "location.longitude": lon,
        "requiredQuality": "LOW",
        "key": GOOGLE_SOLAR_API_KEY
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Extract the best roof segment (highest annual flux)
        roof_segments = data.get("solarPotential", {}).get("roofSegmentStats", [])
        if not roof_segments:
            return _roof_fallback(lat, lon)

        # Primary segment = highest stats pitchDegrees / azimuthDegrees
        solar_potential = data["solarPotential"]
        max_panels      = solar_potential.get("maxArrayPanelsCount", 0)
        max_area_m2     = solar_potential.get("maxArrayAreaMeters2", 0)
        usable_sqft     = max_area_m2 * 10.764

        # Best segment for tilt/azimuth
        best_seg = max(roof_segments, key=lambda s: s.get("stats", {}).get("areaMeters2", 0))
        tilt     = best_seg.get("pitchDegrees", 20.0)
        azimuth  = best_seg.get("azimuthDegrees", 180.0)

        return RoofResult(
            usable_area_sqft=round(usable_sqft, 1),
            max_array_panels=max_panels,
            primary_tilt_deg=round(tilt, 1),
            primary_azimuth_deg=round(azimuth, 1),
            source="Google Solar API (live)",
            raw=data
        )
    except requests.RequestException as e:
        st.warning(f"Google Solar API unavailable ({e}). Using estimated roof values.")
        return _roof_fallback(lat, lon)


def _roof_fallback(lat: float, lon: float) -> RoofResult:
    """
    Fallback roof geometry when Google Solar API key is not set.
    Uses conservative AZ residential defaults. [ASSUMED]
    """
    return RoofResult(
        usable_area_sqft=1000.0,
        max_array_panels=54,
        primary_tilt_deg=18.0,
        primary_azimuth_deg=180.0,
        source="[ASSUMED] Default AZ residential roof (no Google Solar API key set)",
        raw={}
    )


# ─────────────────────────────────────────────
# API 3 — NREL PVWatts v8 (solar production)
# ─────────────────────────────────────────────

def get_pvwatts(lat: float, lon: float, system_kw: float,
                tilt: float, azimuth: float) -> SolarResult:
    """
    Pull annual + monthly AC production from NREL PVWatts v8.
    Free API key: https://developer.nrel.gov/signup/
    DEMO_KEY allows ~50 requests/day, 1,000/hour globally.
    Docs: https://developer.nrel.gov/docs/solar/pvwatts/v8/
    """
    url = "https://developer.nrel.gov/api/pvwatts/v8.json"
    params = {
        "api_key":       NREL_API_KEY,
        "lat":           lat,
        "lon":           lon,
        "system_capacity": system_kw,
        "azimuth":       azimuth,
        "tilt":          tilt,
        "array_type":    1,          # fixed open rack
        "module_type":   1,          # premium module
        "losses":        14,         # system losses %
        "timeframe":     "monthly"
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        outputs       = data.get("outputs", {})
        ac_annual     = outputs.get("ac_annual", 0)
        ac_monthly    = outputs.get("ac_monthly", [0]*12)
        solrad_annual = outputs.get("solrad_annual", 5.5)
        prod_factor   = round(ac_annual / system_kw, 0) if system_kw > 0 else 1750

        return SolarResult(
            ac_annual_kwh=round(ac_annual, 0),
            ac_monthly_kwh=[round(v, 0) for v in ac_monthly],
            peak_sun_hours=round(solrad_annual, 2),
            prod_factor=prod_factor,
            source=f"NREL PVWatts v8 (live) — {NREL_API_KEY}",
            raw=data
        )
    except requests.RequestException as e:
        st.warning(f"PVWatts API unavailable ({e}). Using estimated production values.")
        return _pvwatts_fallback(system_kw)


def _pvwatts_fallback(system_kw: float) -> SolarResult:
    """Fallback PVWatts estimate using AZ average. [ASSUMED]"""
    ac_annual  = system_kw * PROD_FACTOR_AZ
    # Approximate AZ monthly distribution
    monthly_pct = [0.071, 0.075, 0.095, 0.098, 0.101, 0.097,
                   0.091, 0.090, 0.085, 0.082, 0.070, 0.065]
    ac_monthly = [round(ac_annual * p, 0) for p in monthly_pct]
    return SolarResult(
        ac_annual_kwh=round(ac_annual, 0),
        ac_monthly_kwh=ac_monthly,
        peak_sun_hours=5.7,
        prod_factor=PROD_FACTOR_AZ,
        source=f"[ASSUMED] AZ average — PVWatts API unavailable",
        raw={}
    )


# ─────────────────────────────────────────────
# API 4 — OpenEI URDB (utility rate schedule)
# ─────────────────────────────────────────────

def get_utility_rate(address: str, utility_name: str = "") -> RateResult:
    """
    Look up the utility rate schedule from OpenEI Utility Rate Database.
    Free API key: https://openei.org/services/api
    Docs: https://openei.org/services/doc/rest/util_rates/
    """
    url = "https://api.openei.org/utility_rates"
    params = {
        "version":  "latest",
        "api_key":  OPENEI_API_KEY,
        "format":   "json",
        "address":  address,
        "sector":   "Residential",
        "limit":    10,
        "detail":   "full"
    }
    if utility_name:
        params["utility"] = utility_name

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        if not items:
            return _rate_fallback(utility_name)

        # Score and pick best residential match
        best = _pick_best_rate(items, utility_name)
        return _parse_rate(best)

    except requests.RequestException as e:
        st.warning(f"OpenEI API unavailable ({e}). Using estimated rate values.")
        return _rate_fallback(utility_name)


def _pick_best_rate(items: list, utility_name: str) -> dict:
    """
    Score rate items and return the best residential match.
    Preference: name contains utility_name > residential > most recently updated.
    """
    def score(item):
        s = 0
        name = (item.get("name") or "").lower()
        util = (item.get("utility") or "").lower()
        if utility_name and utility_name.lower() in util:
            s += 10
        if "residential" in name or "res" in name:
            s += 5
        if "tou" in name or "time-of-use" in name:
            s += 2
        if item.get("startdate"):
            s += 1
        return s

    return max(items, key=score)


def _parse_rate(item: dict) -> RateResult:
    """Extract key fields from a URDB rate item."""
    # Attempt to pull a flat energy rate from energyratestructure
    energy_rate = 0.0
    is_tou      = False
    structure   = item.get("energyratestructure", [])
    if structure:
        rates = []
        for period in structure:
            for tier in period:
                v = tier.get("rate", 0)
                if v:
                    rates.append(v)
        if rates:
            energy_rate = round(sum(rates) / len(rates), 4)
        if len(structure) > 1:
            is_tou = True

    # Demand charges present?
    has_demand = bool(item.get("demandratestructure"))

    # Net metering
    nm_str = str(item.get("netmetering", "")).lower()
    net_metering = nm_str in ("true", "1", "yes")

    return RateResult(
        utility_name=item.get("utility", "Unknown utility"),
        rate_name=item.get("name", "Unknown rate"),
        rate_id=str(item.get("label", "")),
        energy_rate_kwh=energy_rate if energy_rate > 0 else 0.114,
        is_tou=is_tou,
        has_demand=has_demand,
        net_metering=net_metering,
        source="OpenEI URDB (live)",
        notes=item.get("description", ""),
        raw=item
    )


def _rate_fallback(utility_name: str) -> RateResult:
    """Fallback rate when OpenEI is unavailable. [ASSUMED]"""
    return RateResult(
        utility_name=utility_name or "Unknown utility",
        rate_name="[ASSUMED] Residential TOU (estimate)",
        rate_id="N/A",
        energy_rate_kwh=0.114,
        is_tou=True,
        has_demand=True,
        net_metering=True,
        source="[ASSUMED] — OpenEI API unavailable or no key set",
        notes="SRP E-27 style TOU-demand plan assumed for AZ."
    )


# ─────────────────────────────────────────────
# SIZING ENGINE
# ─────────────────────────────────────────────

def size_system(annual_kwh: float,
                roof: RoofResult,
                solar: SolarResult,
                rate: RateResult,
                battery_goal: str,
                offset_pct: float = 1.0,
                panel_watt: int = PANEL_WATT) -> DesignResult:
    """
    Core sizing logic. All formulas sourced from NREL best practices.
    Every assumption is logged to result.assumptions.
    """
    assumptions = []

    # --- Solar array sizing ---
    target_kwh = annual_kwh * offset_pct
    prod_factor = solar.prod_factor if solar.prod_factor > 0 else PROD_FACTOR_AZ
    system_kw   = round(target_kwh / prod_factor, 2)
    panel_count = math.ceil((system_kw * 1000) / panel_watt)
    area_needed = round(panel_count * PANEL_SQFT, 0)

    assumptions.append(f"Panel wattage: {panel_watt}W per panel")
    assumptions.append(f"Production factor used: {prod_factor:.0f} kWh/kW/yr ({solar.source})")

    # Roof constraint check
    roof_constrained = area_needed > roof.usable_area_sqft
    if roof_constrained:
        max_panels  = int(roof.usable_area_sqft / PANEL_SQFT)
        panel_count = min(panel_count, max_panels)
        system_kw   = round((panel_count * panel_watt) / 1000, 2)
        assumptions.append(f"[ROOF CONSTRAINED] Reduced to {panel_count} panels to fit {roof.usable_area_sqft} sq ft")

    # Refined annual production
    annual_production = round(system_kw * prod_factor, 0)

    # --- Battery sizing ---
    daily_load = annual_kwh / 365
    assumptions.append(f"Daily avg load: {daily_load:.1f} kWh/day")
    assumptions.append(f"Battery: Tesla Powerwall 3 @ {BATTERY_KWH} kWh, {BATTERY_KW} kW, DOD {DOD*100:.0f}%")

    if battery_goal == "Load shifting / TOU arbitrage":
        # Cover ~4–6 hrs of peak window (AZ: 4–9 PM)
        peak_hrs = 5.0
        peak_load_kwh = round(daily_load * (peak_hrs / 24) * 1.3, 1)  # 30% margin
        batteries_needed = math.ceil(peak_load_kwh / (BATTERY_KWH * DOD))
        assumptions.append(f"Peak window: {peak_hrs} hrs, est. peak load {peak_load_kwh} kWh [ASSUMED from daily avg]")

    elif battery_goal == "Backup only (critical loads)":
        critical_kwh = daily_load * 0.35  # ~35% = fridge, lights, router
        batteries_needed = math.ceil(critical_kwh / (BATTERY_KWH * DOD))
        assumptions.append(f"Critical load estimated at 35% of daily avg = {critical_kwh:.1f} kWh [ASSUMED]")

    elif battery_goal == "Whole-home backup (1 day)":
        batteries_needed = math.ceil(daily_load / (BATTERY_KWH * DOD))
        assumptions.append(f"Whole-home 1-day backup target: {daily_load:.1f} kWh")

    else:  # demand reduction default
        batteries_needed = math.ceil((daily_load * 0.5) / (BATTERY_KWH * DOD))
        assumptions.append(f"Demand reduction: targeting 50% of daily load [ASSUMED]")

    batteries_needed  = max(batteries_needed, 1)
    battery_kwh_total = batteries_needed * BATTERY_KWH

    # Self-consumption improvement
    if batteries_needed >= 3:
        self_consumption = 0.90
    elif batteries_needed == 2:
        self_consumption = 0.82
    else:
        self_consumption = 0.70
    assumptions.append(f"Self-consumption {self_consumption*100:.0f}% estimated for {batteries_needed} battery(ies) [ASSUMED]")

    # --- Financials ---
    # Tiered battery pricing: $11,500 first unit, $10,000 each additional
    battery_cost    = 11_500 + max(0, batteries_needed - 1) * 10_000
    solar_cost      = system_kw * 1000 * INSTALL_COST_PER_W
    gross_cost      = round(solar_cost + battery_cost, 0)
    itc_credit      = round(gross_cost * ITC_PCT, 0)
    net_cost        = round(gross_cost - itc_credit, 0)

    annual_savings  = round(annual_production * self_consumption * rate.energy_rate_kwh, 0)
    payback_years   = round(net_cost / annual_savings, 1) if annual_savings > 0 else 99
    year25_savings  = round(annual_savings * 25 - net_cost, 0)

    assumptions.append(f"Install cost: ${INSTALL_COST_PER_W}/W for solar, $12,000/unit for PW3 [ASSUMED national avg]")
    assumptions.append("Federal ITC: Not applicable — residential cash purchase 2026")
    assumptions.append(f"Rate used for savings: ${rate.energy_rate_kwh}/kWh ({rate.source})")
    assumptions.append("25-yr savings uses 4.5% annual utility rate escalator (industry standard AZ)")

    return DesignResult(
        system_kw_dc=system_kw,
        panel_count=panel_count,
        roof_area_needed_sqft=area_needed,
        roof_area_available_sqft=roof.usable_area_sqft,
        roof_constrained=roof_constrained,
        battery_count=batteries_needed,
        battery_kwh_total=battery_kwh_total,
        annual_production_kwh=annual_production,
        self_consumption_pct=self_consumption,
        gross_cost=gross_cost,
        itc_credit=itc_credit,
        net_cost=net_cost,
        payback_years=payback_years,
        annual_savings=annual_savings,
        year25_savings=year25_savings,
        assumptions=assumptions
    )


# ─────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="Solar & Battery Design Tool",
        page_icon="☀️",
        layout="wide",
        initial_sidebar_state="collapsed"
    )

    # ── Header ──────────────────────────────
    st.image("https://images.squarespace-cdn.com/content/v1/69f26e27ec46c6092a14087c/d6884c29-8cd0-453c-b6ce-0f3f4403cccc/honest-energy-logo-light.png?format=300w", width=220)
    st.title("☀️ Solar & Battery Design Tool")
    st.caption("Free solar + storage system sizing using real API data. All assumptions are labeled.")
    st.divider()

    # ── Input Form ──────────────────────────
    with st.form("design_form"):
        st.subheader("Property Information")
        col1, col2 = st.columns(2)

        with col1:
            address = st.text_input(
                "Street address",
                placeholder="Enter your full street address, city, state, zip"
            )
            utility_name = st.text_input(
                "Utility company (optional, improves rate lookup)",
                placeholder="SRP, APS, PG&E, etc."
            )
            annual_kwh = st.number_input(
                "Annual electricity usage (kWh)",
                min_value=1000, max_value=100000,
                value=12000, step=500,
                help="Find this on your electric bill or enter 12 × monthly avg"
            )

        with col2:
            battery_goal = st.selectbox(
                "Battery goal",
                options=[
                    "Load shifting / TOU arbitrage",
                    "Backup only (critical loads)",
                    "Whole-home backup (1 day)",
                    "Demand reduction"
                ]
            )
            offset_pct = st.slider(
                "Solar offset target (%)",
                min_value=50, max_value=120, value=100, step=5,
                help="100% = design to cover 100% of annual usage"
            )
            panel_watts_override = st.number_input(
                "Panel wattage (override)",
                min_value=300, max_value=700,
                value=PANEL_WATT, step=10,
                help="Default 400W. Adjust for specific panel selection."
            )

        st.caption("Fields marked [ASSUMED] in results mean we estimated due to missing data or API unavailability.")
        submitted = st.form_submit_button("⚡ Run Design", type="primary", use_container_width=True)

    # ── Run Design ──────────────────────────
    if submitted:
        if not address.strip():
            st.error("Please enter a property address.")
            st.stop()

        progress = st.progress(0, text="Starting design run...")

        try:
            # Step 1: Geocode
            progress.progress(10, text="📍 Geocoding address...")
            geo = geocode_address(address)
            st.success(f"📍 Matched: {geo.formatted_address}  |  Lat {geo.lat:.4f}, Lon {geo.lon:.4f}")

            # Step 2: Roof geometry
            progress.progress(30, text="🏠 Pulling roof geometry...")
            roof = get_roof_geometry(geo.lat, geo.lon)

            # Step 3: PVWatts — initial run with default system size, refine after sizing
            progress.progress(50, text="☀️ Fetching solar irradiance from NREL PVWatts...")
            rough_kw = round(annual_kwh / PROD_FACTOR_AZ, 1)
            solar = get_pvwatts(geo.lat, geo.lon, rough_kw,
                                tilt=roof.primary_tilt_deg,
                                azimuth=roof.primary_azimuth_deg)

            # Step 4: Utility rates
            progress.progress(70, text="💡 Looking up utility rate schedule...")
            rate = get_utility_rate(address, utility_name)

            # Step 5: Size the system
            progress.progress(85, text="🔢 Running sizing calculations...")
            design = size_system(
                annual_kwh=annual_kwh,
                roof=roof,
                solar=solar,
                rate=rate,
                battery_goal=battery_goal,
                offset_pct=offset_pct / 100,
                panel_watt=int(panel_watts_override)
            )

            # Re-run PVWatts with actual system size
            solar = get_pvwatts(geo.lat, geo.lon, design.system_kw_dc,
                                tilt=roof.primary_tilt_deg,
                                azimuth=roof.primary_azimuth_deg)

            progress.progress(100, text="✅ Design complete.")
            progress.empty()

            # ── Results ──────────────────────────
            st.divider()
            st.subheader("Design Results")

            # KPI cards — top row
            kpi1, kpi2, kpi3, kpi4 = st.columns(4)
            kpi1.metric("System size", f"{design.system_kw_dc} kW DC")
            kpi2.metric("Panel count", f"{design.panel_count} panels ({panel_watts_override}W)")
            kpi3.metric("Battery storage", f"{design.battery_count}× Powerwall 3  ({design.battery_kwh_total} kWh)")
            kpi4.metric("Annual production", f"{design.annual_production_kwh:,.0f} kWh")

            kpi5, kpi6, kpi7, kpi8 = st.columns(4)
            kpi5.metric("Gross system cost", f"${design.gross_cost:,.0f}")
            kpi6.metric("AZ State Incentives", "See details below")
            kpi7.metric("Net Contract Cost", f"${design.net_cost:,.0f}")
            kpi8.metric("Simple payback", f"{design.payback_years} years")

            st.divider()

            # Detailed sections
            tab1, tab2, tab3, tab4, tab5 = st.tabs([
                "🏠 Roof & Site", "☀️ Solar Production", "💡 Utility Rates",
                "🔋 Battery Design", "📋 Assumptions & Sources"
            ])

            with tab1:
                st.markdown("### Roof & Site Details")
                c1, c2 = st.columns(2)
                c1.markdown(f"""
| Field | Value |
|---|---|
| Usable roof area | {roof.usable_area_sqft:,.0f} sq ft |
| Max panels (roof) | {roof.max_array_panels} |
| Primary tilt | {roof.primary_tilt_deg}° |
| Primary azimuth | {roof.primary_azimuth_deg}° (180° = south) |
| Roof area needed | {design.roof_area_needed_sqft:,.0f} sq ft |
| Roof constrained? | {"⚠️ Yes — system reduced to fit" if design.roof_constrained else "✅ No — fits comfortably"} |
""")
                c2.markdown(f"""
**Data source:** {roof.source}

**Coordinates:** {geo.lat:.5f}, {geo.lon:.5f}

**Address matched:** {geo.formatted_address}
""")

            with tab2:
                st.markdown("### Solar Production Estimate")
                c1, c2 = st.columns(2)
                c1.markdown(f"""
| Month | Est. Production (kWh) |
|---|---|
| January   | {solar.ac_monthly_kwh[0]:,.0f} |
| February  | {solar.ac_monthly_kwh[1]:,.0f} |
| March     | {solar.ac_monthly_kwh[2]:,.0f} |
| April     | {solar.ac_monthly_kwh[3]:,.0f} |
| May       | {solar.ac_monthly_kwh[4]:,.0f} |
| June      | {solar.ac_monthly_kwh[5]:,.0f} |
| July      | {solar.ac_monthly_kwh[6]:,.0f} |
| August    | {solar.ac_monthly_kwh[7]:,.0f} |
| September | {solar.ac_monthly_kwh[8]:,.0f} |
| October   | {solar.ac_monthly_kwh[9]:,.0f} |
| November  | {solar.ac_monthly_kwh[10]:,.0f} |
| December  | {solar.ac_monthly_kwh[11]:,.0f} |
| **Annual total** | **{solar.ac_annual_kwh:,.0f}** |
""")
                c2.markdown(f"""
**Peak sun hours/day:** {solar.peak_sun_hours}

**Production factor:** {solar.prod_factor:,.0f} kWh/kW/yr

**System size used:** {design.system_kw_dc} kW DC

**Self-consumption with battery:** {design.self_consumption_pct*100:.0f}%

**Data source:** {solar.source}
""")
                # Monthly bar chart
                import pandas as pd
                months = ["Jan","Feb","Mar","Apr","May","Jun",
                          "Jul","Aug","Sep","Oct","Nov","Dec"]
                df = pd.DataFrame({"Month": months, "kWh": solar.ac_monthly_kwh})
                st.bar_chart(df.set_index("Month"))

            with tab3:
                st.markdown("### Utility Rate Schedule")
                st.markdown(f"""
| Field | Value |
|---|---|
| Utility | {rate.utility_name} |
| Rate schedule | {rate.rate_name} |
| Rate ID | {rate.rate_id} |
| Blended energy rate | ${rate.energy_rate_kwh}/kWh |
| TOU pricing | {"✅ Yes" if rate.is_tou else "❌ No"} |
| Demand charges | {"✅ Yes — battery dispatch critical" if rate.has_demand else "❌ No"} |
| Net metering | {"✅ Yes" if rate.net_metering else "⚠️ Not confirmed"} |
""")
                st.caption(f"Source: {rate.source}")
                if rate.notes:
                    st.info(f"Rate notes: {rate.notes[:300]}")
                if rate.is_tou:
                    st.warning("**TOU rate detected.** Battery dispatch timing is critical. "
                               "Confirm your exact peak/off-peak windows with your utility.")
                if rate.has_demand:
                    st.warning("**Demand charges present.** Battery should be configured to "
                               "clip demand spikes — this is where the biggest savings may be.")

            with tab4:
                st.markdown("### Battery Design")
                st.markdown(f"""
| Field | Value |
|---|---|
| Battery goal | {battery_goal} |
| Battery model | Tesla Powerwall 3 |
| Batteries recommended | {design.battery_count} units |
| Total storage | {design.battery_kwh_total} kWh |
| Total power output | {design.battery_count * BATTERY_KW:.1f} kW continuous |
| Depth of discharge | {DOD*100:.0f}% |
| Round-trip efficiency | {RTE*100:.0f}% |
| Est. self-consumption | {design.self_consumption_pct*100:.0f}% of production |
""")
                st.markdown("### 25-Year Financial Summary")
                st.markdown(f"""
| Field | Value |
|---|---|
| Gross system cost | ${design.gross_cost:,.0f} |
| Federal ITC | Not applicable (residential cash purchase 2026) |
| **Net Contract Cost** | **${design.net_cost:,.0f}** |
| Annual bill savings (est.) | ${design.annual_savings:,.0f}/yr |
| Simple payback | {design.payback_years} years |
| 25-yr net savings | ${design.year25_savings:,.0f} |
""")

            with tab5:
                st.markdown("### All Assumptions & Data Sources")
                st.markdown("Every line flagged below is an estimate — replace with real data when available.")
                for i, a in enumerate(design.assumptions, 1):
                    st.markdown(f"{i}. {a}")
                st.divider()
                st.markdown("**Data sources used in this run:**")
                st.markdown(f"- Geocoding: US Census Bureau Geocoder (no key)")
                st.markdown(f"- Roof geometry: {roof.source}")
                st.markdown(f"- Solar production: {solar.source}")
                st.markdown(f"- Utility rate: {rate.source}")

            # ── Download ──────────────────────────
            st.divider()
            report_text = _build_text_report(geo, roof, solar, rate, design,
                                             address, annual_kwh, battery_goal)
            st.download_button(
                label="📄 Download Report (.txt)",
                data=report_text,
                file_name="solar_battery_design_report.txt",
                mime="text/plain",
                use_container_width=True
            )

        except (ValueError, ConnectionError) as e:
            progress.empty()
            st.error(str(e))
        except Exception as e:
            progress.empty()
            st.error(f"Unexpected error: {e}")
            st.exception(e)


# ─────────────────────────────────────────────
# REPORT BUILDER
# ─────────────────────────────────────────────

def _build_text_report(geo, roof, solar, rate, design,
                       address, annual_kwh, battery_goal) -> str:
    lines = [
        "=" * 60,
        "  SOLAR & BATTERY DESIGN REPORT",
        "=" * 60,
        f"Property:         {address}",
        f"Matched address:  {geo.formatted_address}",
        f"Coordinates:      {geo.lat:.5f}, {geo.lon:.5f}",
        f"Annual usage:     {annual_kwh:,} kWh",
        f"Battery goal:     {battery_goal}",
        "",
        "── ROOF & SITE ─────────────────────────────",
        f"Usable area:      {roof.usable_area_sqft:,.0f} sq ft",
        f"Primary tilt:     {roof.primary_tilt_deg}°",
        f"Primary azimuth:  {roof.primary_azimuth_deg}°",
        f"Source:           {roof.source}",
        "",
        "── SOLAR PRODUCTION ────────────────────────",
        f"System size:      {design.system_kw_dc} kW DC",
        f"Panel count:      {design.panel_count} panels",
        f"Annual output:    {solar.ac_annual_kwh:,.0f} kWh",
        f"Peak sun hours:   {solar.peak_sun_hours}/day",
        f"Source:           {solar.source}",
        "",
        "── UTILITY RATE ────────────────────────────",
        f"Utility:          {rate.utility_name}",
        f"Rate schedule:    {rate.rate_name}",
        f"Blended rate:     ${rate.energy_rate_kwh}/kWh",
        f"TOU:              {'Yes' if rate.is_tou else 'No'}",
        f"Demand charges:   {'Yes' if rate.has_demand else 'No'}",
        f"Net metering:     {'Yes' if rate.net_metering else 'Not confirmed'}",
        f"Source:           {rate.source}",
        "",
        "── BATTERY DESIGN ──────────────────────────",
        f"Model:            Tesla Powerwall 3",
        f"Count:            {design.battery_count} units",
        f"Total storage:    {design.battery_kwh_total} kWh",
        f"Total power:      {design.battery_count * BATTERY_KW:.1f} kW continuous",
        "",
        "── FINANCIALS ──────────────────────────────",
        f"Gross cost:       ${design.gross_cost:,.0f}",
        f"Federal ITC:      -${design.itc_credit:,.0f}",
        f"Net cost:         ${design.net_cost:,.0f}",
        f"Annual savings:   ${design.annual_savings:,.0f}/yr",
        f"Simple payback:   {design.payback_years} years",
        f"25-yr net savings:${design.year25_savings:,.0f}",
        "",
        "── ASSUMPTIONS ─────────────────────────────",
    ]
    for i, a in enumerate(design.assumptions, 1):
        lines.append(f"  {i}. {a}")
    lines += ["", "=" * 60,
              "Generated by Solar & Battery Design Tool",
              "All [ASSUMED] values should be verified before contracting.",
              "=" * 60]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main()
