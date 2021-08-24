"""Prepare data for water use for cooling & energy technologies."""

import pandas as pd
from message_ix import make_df
from message_ix_models.util import (
    broadcast,
    make_matched_dfs,
    private_data_path,
    same_node,
)


# water & electricity for cooling technologies
def cool_tech(context):
    """Process cooling technology data for a scenario instance.

    The input values of parent technologies are read in from a scenario instance and
    then cooling fractions are calculated by using the data from
    ``tech_water_performance_ssp_msg.csv``.
    It adds cooling  technologies as addons to the parent technologies.The
    nomenclature for cooling technology is <parenttechnologyname>__<coolingtype>.
    E.g: `coal_ppl__ot_fresh`

    Parameters
    ----------
    context : .Context

    Returns
    -------
    data : dict of (str -> pandas.DataFrame)
        Keys are MESSAGE parameter names such as 'input', 'fix_cost'.
        Values are data frames ready for :meth:`~.Scenario.add_par`.
        Years in the data include the model horizon indicated by
        ``context["water build info"]``, plus the additional year 2010.
    """
    #: Name of the input file.
    # The input file mentions water withdrawals and emission heating fractions for
    # cooling technologies alongwith parent technologies:
    FILE = "tech_water_performance_ssp_msg.csv"
    # Investment costs & regional shares of hist. activities of cooling
    # technologies
    FILE1 = (
        "cooltech_cost_and_shares_"
        + (f"ssp_msg_{context.regions}" if context.type_reg == "global" else "country")
        + ".csv"
    )

    # define an empty dictionary
    results = {}

    # Reference to the water configuration
    info = context["water build info"]

    # reading basin_delineation
    path1 = private_data_path("water", "delineation", "basins_by_region_simpl_R11.csv")
    df_node = pd.read_csv(path1)
    # Assigning proper nomenclature
    df_node["node"] = "B" + df_node["BCU_name"].astype(str)
    df_node["mode"] = "M" + df_node["BCU_name"].astype(str)
    df_node["region"] = "R11_" + df_node["REGION"].astype(str)

    # reading ppl cooling tech dataframe
    path = private_data_path("water", "ppl_cooling_tech", FILE)
    df = pd.read_csv(path)
    cooling_df = df.loc[df["technology_group"] == "cooling"]
    # Separate a column for parent technologies of respective cooling
    # techs
    cooling_df["parent_tech"] = (
        cooling_df["technology_name"]
        .apply(lambda x: pd.Series(str(x).split("__")))
        .drop(columns=1)
    )

    scen = context.get_scenario()

    # Extracting input database from scenario for parent technologies
    # Extracting input values from scenario
    ref_input = scen.par("input", {"technology": cooling_df["parent_tech"]})
    # Extracting historical activity from scenario
    ref_hist_act = scen.par(
        "historical_activity", {"technology": cooling_df["parent_tech"]}
    )
    # Extracting historical capacity from scenario
    ref_hist_cap = scen.par(
        "historical_new_capacity", {"technology": cooling_df["parent_tech"]}
    )

    # cooling fraction = H_cool = Hi - 1 - Hi*(h_fg)
    # where h_fg (flue gasses losses) = 0.1
    ref_input["cooling_fraction"] = ref_input["value"] * 0.9 - 1

    def missing_tech(x):
        """Assign values to missing data.
        It goes through the input data frame and extract the technologies which
        don't have input values and then assign manual  values to those technologies
        along with assigning them an arbitrary level i.e dummy supply
        """
        data_dic = {
            "geo_hpl": 1 / 0.850,
            "geo_ppl": 1 / 0.385,
            "nuc_hc": 1 / 0.326,
            "nuc_lc": 1 / 0.326,
            "solar_th_ppl": 1 / 0.385,
        }

        if data_dic.get(x["technology"]):
            if x["level"] == "cooling":
                return pd.Series((data_dic.get(x["technology"]), "dummy_supply"))
            else:
                return pd.Series((data_dic.get(x["technology"]), x["level"]))
        else:
            return pd.Series((x["value"], x["level"]))

    ref_input[["value", "level"]] = ref_input[["technology", "value", "level"]].apply(
        missing_tech, axis=1
    )

    # Combines the input df of parent_tech with water withdrawal data
    input_cool = (
        cooling_df.set_index("parent_tech")
        .combine_first(ref_input.set_index("technology"))
        .reset_index()
    )

    # Drops NA values from the value column
    input_cool = input_cool.dropna(subset=["value"])

    # Convert year values into integers to be compatibel for model
    input_cool.year_vtg = input_cool.year_vtg.astype(int)
    input_cool.year_act = input_cool.year_act.astype(int)
    # Drops extra technologies from the data
    input_cool = input_cool[
        (input_cool["level"] != "water_supply") & (input_cool["level"] != "cooling")
    ]

    input_cool = input_cool[
        ~input_cool["technology_name"].str.contains("hpl", na=False)
    ]

    def cooling_fr(x):
        """Calculate cooling fraction

        Returns the calculated cooling fraction after for two categories;
        1. Technologies that produce heat as an output
            cooling_fraction(h_cool) = input value(hi) - 1
        Simply subtract 1 from the heating value since the rest of the part is already
        accounted in the heating value

        2. Rest of technologies
            h_cool  =  hi -Hi* h_fg - 1,
            where:
                h_fg (flue gasses losses) = 0.1 (10% assumed losses)
        """
        if "hpl" in x["index"]:
            return x["value"] - 1
        else:
            return x["value"] - (x["value"] * 0.1) - 1

    input_cool["cooling_fraction"] = input_cool.apply(cooling_fr, axis=1)

    # Converting water withdrawal units to Km3/GWa
    input_cool["value_cool"] = (
        input_cool["water_withdrawal_mid_m3_per_output"]
        * 60
        * 60
        * 24
        * 365
        * 1e-9
        / input_cool["cooling_fraction"]
    )

    # def foo3(x):
    #     """
    #     This function is similar to foo2, but it returns electricity values
    #     per unit of cooling for techs that require parasitic electricity demand
    #     """
    #     if "hpl" in x['index']:
    #         return x['parasitic_electricity_demand_fraction']
    #
    #     elif x['parasitic_electricity_demand_fraction'] > 0.0:
    #         return x['parasitic_electricity_demand_fraction'] / x['cooling_fraction']

    # Filter out technologies that requires parasitic electricity
    electr = input_cool[input_cool["parasitic_electricity_demand_fraction"] > 0.0]

    # Make a new column 'value_cool' for calculating values against technologies
    electr["value_cool"] = (
        electr["parasitic_electricity_demand_fraction"] / electr["cooling_fraction"]
    )
    # Filters out technologies requiring saline water supply
    saline_df = input_cool[
        input_cool["technology_name"].str.endswith("ot_saline", na=False)
    ]

    # input_cool_minus_saline_elec_df
    con1 = input_cool["technology_name"].str.endswith("ot_saline", na=False)
    con2 = input_cool["technology_name"].str.endswith("air", na=False)
    icmse_df = input_cool[(~con1) & (~con2)]

    inp = make_df(
        "input",
        node_loc=electr["node_loc"],
        technology=electr["technology_name"],
        year_vtg=electr["year_vtg"],
        year_act=electr["year_act"],
        mode=electr["mode"],
        node_origin=electr["node_origin"],
        commodity="electr",
        level="secondary",
        time="year",
        time_origin="year",
        value=electr["value_cool"],
        unit="GWa",
    )

    inp = inp.append(
        make_df(
            "input",
            node_loc=icmse_df["node_loc"],
            technology=icmse_df["technology_name"],
            year_vtg=icmse_df["year_vtg"],
            year_act=icmse_df["year_act"],
            mode=icmse_df["mode"],
            node_origin=icmse_df["node_origin"],
            commodity="freshwater_supply",
            level="water_supply",
            time="year",
            time_origin="year",
            value=icmse_df["value_cool"],
            unit="km3/GWa",
        )
    )

    inp = inp.append(
        make_df(
            "input",
            node_loc=saline_df["node_loc"],
            technology=saline_df["technology_name"],
            year_vtg=saline_df["year_vtg"],
            year_act=saline_df["year_act"],
            mode=saline_df["mode"],
            node_origin=saline_df["node_origin"],
            commodity="saline_supply_ppl",
            level="water_supply",
            time="year",
            time_origin="year",
            value=saline_df["value_cool"],
            unit="km3/GWa",
        )
    )

    # Drops NA values from the value column
    inp = inp.dropna(subset=["value"])

    # append the input data to results
    results["input"] = inp

    path1 = private_data_path("water", "ppl_cooling_tech", FILE1)
    cost = pd.read_csv(path1)
    # Combine technology name to get full cooling tech names
    cost["technology"] = cost["utype"] + "__" + cost["cooling"]
    # Filtering out 2010 data to use for historical values
    input_cool_2010 = input_cool[
        (input_cool["year_act"] == 2010) & (input_cool["year_vtg"] == 2010)
    ]
    # Filter out columns that contain 'mix' in column name
    columns = [col for col in cost.columns if "mix_" in col]
    # Rename column names to R11 to match with the previous df

    cost.rename(columns=lambda name: name.replace("mix_", ""), inplace=True)
    search_cols = [
        col for col in cost.columns if context.regions in col or "technology" in col
    ]
    hold_df = input_cool_2010[
        ["node_loc", "technology_name", "cooling_fraction"]
    ].drop_duplicates()
    search_cols_cooling_fraction = [col for col in search_cols if col != "technology"]

    def shares(x, context):
        """Process share and cooling fraction.

        Returns
        -------
        Product of value of shares of cooling technology types of regions with
        corresponding cooling fraction
        """
        for col in search_cols_cooling_fraction:
            # MAPPING ISOCODE to region name, assume one country only
            col2 = context.map_ISO_c[col] if context.type_reg == "country" else col
            cooling_fraction = hold_df[
                (hold_df["node_loc"] == col2)
                & (hold_df["technology_name"] == x["technology"])
            ]["cooling_fraction"]
            x[col] = x[col] * cooling_fraction

        results = []
        for i in x:
            if isinstance(i, str):
                results.append(i)
            else:
                if not len(i):
                    return pd.Series(
                        [i for i in range(len(search_cols) - 1)] + ["delme"],
                        index=search_cols,
                    )
                else:
                    results.append(float(i))
        return pd.Series(results, index=search_cols)

    # Apply function to the
    hold_cost = cost[search_cols].apply(shares, axis=1, context=context)
    hold_cost = hold_cost[hold_cost["technology"] != "delme"]

    def hist_act(x, context):
        """Calculate historical activity of cooling technology.
        The data for shares is read from ``cooltech_cost_and_shares_ssp_msg.csv``

        Returns
        -------
        hist_activity(cooling_tech) = hist_activitiy(parent_technology) * share
        *cooling_fraction
        """
        tech_df = hold_cost[
            hold_cost["technology"].str.startswith(x.technology)
        ]  # [x.node_loc]

        node_search = (
            context.regions if context.type_reg == "country" else x["node_loc"]
        )

        node_loc = x["node_loc"]
        technology = x["technology"]
        cooling_technologies = list(tech_df["technology"])
        new_values = tech_df[node_search] * x.value

        return [
            [
                node_loc,
                technology,
                cooling_technology,
                x.year_act,
                x.value,
                new_value,
                x.unit,
            ]
            for new_value, cooling_technology in zip(new_values, cooling_technologies)
        ]

    changed_value_series = ref_hist_act.apply(hist_act, axis=1, context=context)
    changed_value_series_flat = [
        row for series in changed_value_series for row in series
    ]
    columns = [
        "node_loc",
        "technology",
        "cooling_technology",
        "year_act",
        "value",
        "new_value",
        "unit",
    ]
    # dataframe for historical activities of cooling techs
    act_value_df = pd.DataFrame(changed_value_series_flat, columns=columns)

    def hist_cap(x, context):
        """Calculate historical capacity of cooling technology.
        The data for shares is read from ``cooltech_cost_and_shares_ssp_msg.csv``
        Returns
        -------
        hist_new_capacity(cooling_tech) = historical_new_capacity(parent_technology)*
        share * cooling_fraction
        """
        tech_df = hold_cost[
            hold_cost["technology"].str.startswith(x.technology)
        ]  # [x.node_loc]
        if context.type_reg == "country":
            node_search = context.regions
        else:
            node_search = x["node_loc"]  # R11_EEU
        node_loc = x["node_loc"]
        technology = x["technology"]
        cooling_technologies = list(tech_df["technology"])
        new_values = tech_df[node_search] * x.value

        return [
            [
                node_loc,
                technology,
                cooling_technology,
                x.year_vtg,
                x.value,
                new_value,
                x.unit,
            ]
            for new_value, cooling_technology in zip(new_values, cooling_technologies)
        ]

    changed_value_series = ref_hist_cap.apply(hist_cap, axis=1, context=context)
    changed_value_series_flat = [
        row for series in changed_value_series for row in series
    ]
    columns = [
        "node_loc",
        "technology",
        "cooling_technology",
        "year_vtg",
        "value",
        "new_value",
        "unit",
    ]
    cap_value_df = pd.DataFrame(changed_value_series_flat, columns=columns)

    # Make model compatible df for histroical acitvitiy
    h_act = make_df(
        "historical_activity",
        node_loc=act_value_df["node_loc"],
        technology=act_value_df["cooling_technology"],
        year_act=act_value_df["year_act"],
        mode="M1",
        time="year",
        value=act_value_df["new_value"],
        # TODO finalize units
        unit="km3/GWa",
    )

    results["historical_activity"] = h_act
    # Make model compatible df for histroical new capacity
    h_cap = make_df(
        "historical_new_capacity",
        node_loc=cap_value_df["node_loc"],
        technology=cap_value_df["cooling_technology"],
        year_vtg=cap_value_df["year_vtg"],
        value=cap_value_df["new_value"],
        unit="km3/GWa",
    )

    results["historical_new_capacity"] = h_cap

    # Filter out just cl_fresh & air technologies for adding inv_cost in model,
    # The rest fo technologies are assumed to have costs included in parent technologies
    # con3 = cost['technology'].str.endswith("cl_fresh")
    # con4 = cost['technology'].str.endswith("air")
    # con5 = cost.technology.isin(input_cool['technology_name'])
    # inv_cost = cost[(con3) | (con4)]
    inv_cost = cost.copy()
    # Manually removing extra technolgoies not required
    # TODO make it automatic to not include the names manually
    techs_to_remove = [
        "mw_ppl__ot_fresh",
        "mw_ppl__ot_saline",
        "mw_ppl__cl_fresh",
        "mw_ppl__air",
        "nuc_fbr__ot_fresh",
        "nuc_fbr__ot_saline",
        "nuc_fbr__cl_fresh",
        "nuc_fbr__air",
        "nuc_htemp__ot_fresh",
        "nuc_htemp__ot_saline",
        "nuc_htemp__cl_fresh",
        "nuc_htemp__air",
    ]
    inv_cost = inv_cost[~inv_cost["technology"].isin(techs_to_remove)]
    # Converting the cost to USD/GW
    inv_cost["investment_USD_per_GW_mid"] = (
        inv_cost["investment_million_USD_per_MW_mid"] * 1e3
    )

    inv_cost = (
        make_df(
            "inv_cost",
            technology=inv_cost["technology"],
            value=inv_cost["investment_USD_per_GW_mid"],
            unit="USD/GWa",
        )
        .pipe(same_node)
        .pipe(broadcast, node_loc=info.N[1:13], year_vtg=info.Y)
    )

    results["inv_cost"] = inv_cost

    # Addon conversion
    adon_df = input_cool.copy()
    # Add 'cooling_' before name of parent technologies that are type_addon
    # nomenclature
    adon_df["tech"] = "cooling__" + adon_df["index"].astype(str)
    # technology : 'parent technology' and type_addon is type of addons such
    # as 'cooling__bio_hpl'
    addon_df = make_df(
        "addon_conversion",
        node=adon_df["node_loc"],
        technology=adon_df["index"],
        year_vtg=adon_df["year_vtg"],
        year_act=adon_df["year_act"],
        mode=adon_df["mode"],
        time="year",
        type_addon=adon_df["tech"],
        value=adon_df["cooling_fraction"],
        unit="km3/GWa",
    )

    results["addon_conversion"] = addon_df

    # Addon_lo will remain 1 for all cooling techs so it allows 100% activity of
    # parent technologies
    addon_lo = make_matched_dfs(addon_df, addon_lo=1)
    results["addon_lo"] = addon_lo["addon_lo"]

    # technical lifetime
    # make_matched_dfs didn't map all technologies
    # tl = make_matched_dfs(inv_cost,
    #                       technical_lifetime = 30)
    year = info.Y
    if 2010 in year:
        pass
    else:
        year.insert(0, 2010)

    tl = (
        make_df(
            "technical_lifetime",
            technology=inp["technology"].drop_duplicates(),
            value=30,
            unit="year",
        )
        .pipe(broadcast, year_vtg=year, node_loc=info.N[1:13])
        .pipe(same_node)
    )

    results["technical_lifetime"] = tl

    if context.nexus_set == 'cooling':
        # Add output df  for freshwater supply for regions
        output_df = (
            make_df(
                "output",
                technology="extract_freshwater_supply",
                value=1,
                unit="km3",
                year_vtg=info.Y,
                year_act=info.Y,
                level="water_supply",
                commodity="freshwater_supply",
                mode="M1",
                time="year",
                time_dest="year",
                time_origin="year",
            )
            .pipe(broadcast, node_loc=info.N[1:13])
            .pipe(same_node)
        )
        # Add output of saline water supply for regions
        output_df = output_df.append((
            make_df(
                "output",
                technology="extract_saline_supply",
                value=1,
                unit="km3",
                year_vtg=info.Y,
                year_act=info.Y,
                level="water_supply",
                commodity="saline_supply_ppl",
                mode="M1",
                time="year",
                time_dest="year",
                time_origin="year",
            )
                .pipe(broadcast, node_loc=info.N[1:13])
                .pipe(same_node)
        )
        )

        results["output"] = output_df


    cap_fact = make_matched_dfs(inp, capacity_factor=1)
    results["capacity_factor"] = cap_fact["capacity_factor"]
    # results = {par_name: pd.concat(dfs) for par_name, dfs in results.items()}
    if context.nexus_set == "nexus":
        # Add output df  for freshwater supply for basins
        output_df = make_df(
                "output",
                technology="extract_freshwater_supply",
                value=1,
                unit="-",
                level="water_supply_basin",
                commodity="freshwater_supply_basin",
                mode='M1',
                node_loc=df_node["node"],
                node_dest=df_node["node"],
                time="year",
                time_dest="year",
                time_origin="year",
            ).pipe(
                broadcast,
                year_vtg=info.Y,
                year_act=info.Y,
            )
        # Add output of saline water supply for regions
        output_df = output_df.append((
            make_df(
                "output",
                technology="extract_saline_supply",
                value=1,
                unit="km3",
                year_vtg=info.Y,
                year_act=info.Y,
                level="water_supply",
                commodity="saline_supply_ppl",
                mode="M1",
                time="year",
                time_dest="year",
                time_origin="year",
            )
                .pipe(broadcast, node_loc=info.N[1:13])
                .pipe(same_node)
        )
        )

        # output dataframe linking water supply to energy dummy technology
        output_df = output_df.append(
            make_df(
            "output",
            technology="water_to_en",
            value=1,
            unit="-",
            level="water_supply",
            commodity="freshwater_supply",
            mode=df_node["mode"],
            node_loc=df_node["region"],
            node_dest=df_node["region"],
            time="year",
            time_dest="year",
            time_origin="year",
        ).pipe(
            broadcast,
            year_vtg=info.Y,
            year_act=info.Y,
        )
        )

        results["output"] = output_df

        # input dataframe  linking water supply to energy dummy technology
        inp = make_df(
            "input",
            technology="water_to_en",
            value=1,
            unit="-",
            level="water_supply_basin",
            commodity="freshwater_supply_basin",
            mode=df_node["mode"],
            time="year",
            time_origin="year",
            node_origin=df_node["node"],
            node_loc=df_node["region"],
        ).pipe(broadcast, year_vtg=info.Y, year_act=info.Y)
        results["input"] = inp

        # dummy variable cost for dummy water to energy technology
        var = make_df(
            "var_cost",
            technology="water_to_en",
            mode=df_node["mode"],
            node_loc=df_node["region"],
            time="year",
            value=20,
            unit="-",
        ).pipe(broadcast, year_vtg=info.Y, year_act=info.Y)
        results["var_cost"] = var

        # reading flow availability data for defining share constraints of basins in each recgion
        path2 = private_data_path(
            "water", "water_availability", "run_off_rcp26_mean.csv"
        )
        df_wat = pd.read_csv(path2)
        df_wat["region"] = "R11_" + df_wat["BCU_name"].str[-3:]
        df_wat_1 = df_wat.drop(
            columns=[
                "Unnamed: 0",
                "BASIN_ID",
                "unit",
                "BCU_name",
                "2065",
                "2075",
                "2085",
                "2095",
            ],
            axis=1,
        )
        df_wat_1["2110"] = 1.1 * df_wat_1["2100"]
        # Calculating ratio of water availability in basin by region
        df_wat_1 = df_wat_1.groupby(["region"]).apply(lambda x: x / x.sum())
        df_wat_1["node"] = "B" + df_wat["BCU_name"].astype(str)
        df_wat_1["mode"] = "M" + df_node["BCU_name"].astype(str)
        df_wat_1["region"] = df_wat["region"].astype(str)
        # restacking dataframe to be compatible with MESSAGE format
        df_wat_1 = df_wat_1.set_index(["node", "region", "mode"])
        df_wat_1 = df_wat_1.stack().reset_index(level=0).reset_index()
        df_wat_1.columns = ["region", "mode", "year", "node", "value"]
        df_wat_1.sort_values(["region", "mode", "year", "node", "value"], inplace=True)

        share = make_df(
            "share_mode_up",
            shares="share_basin",
            technology="water_to_en",
            mode=df_wat_1["mode"],
            node_share=df_wat_1["region"],
            time="year",
            value=df_wat_1["value"],
            unit="-",
            year_act=df_wat_1["year"],
        )
        results["share_mode_up"] = share

    return results


# Water use & electricity for non-cooling technologies
def non_cooling_tec(context):
    """Process data for water usage of power plants (non-cooling technology related).

    Water withdrawal values for power plants are read in from
    ``tech_water_performance_ssp_msg.csv``

    Parameters
    ----------
    context : .Context

    Returns
    -------
    data : dict of (str -> pandas.DataFrame)
        Keys are MESSAGE parameter names such as 'input', 'fix_cost'.
        Values are data frames ready for :meth:`~.Scenario.add_par`.
        Years in the data include the model horizon indicated by
        ``context["transport build info"]``, plus the additional year 2010.
    """
    results = {}

    info = context["water build info"]
    FILE = "tech_water_performance_ssp_msg.csv"
    path = private_data_path("water", "ppl_cooling_tech", FILE)
    df = pd.read_csv(path)
    cooling_df = df.loc[df["technology_group"] == "cooling"]
    # Separate a column for parent technologies of respective cooling
    # techs
    cooling_df["parent_tech"] = (
        cooling_df["technology_name"]
        .apply(lambda x: pd.Series(str(x).split("__")))
        .drop(columns=1)
    )
    non_cool_df = df[
        (df["technology_group"] != "cooling")
        & (df["water_supply_type"] == "freshwater_supply")
    ]

    scen = context.get_scenario()
    tec_lt = scen.par('technical_lifetime')
    all_tech = list(tec_lt['technology'].unique())
    #all_tech = list(scen.set("technology"))
    tech_non_cool_csv = list(non_cool_df["technology_name"])
    techs_to_remove = [tec for tec in tech_non_cool_csv if tec not in all_tech]

    non_cool_df = non_cool_df[~non_cool_df["technology_name"].isin(techs_to_remove)]
    non_cool_df = non_cool_df.rename(columns={'technology_name': 'technology'})

    non_cool_df["value"] = (
        non_cool_df["water_withdrawal_mid_m3_per_output"] * 60 * 60 * 24 * 365 * (1e-9)
    )

    non_cool_tech = list(non_cool_df['technology'].unique())

    n_cool_df = scen.par('output', {'technology': non_cool_tech})
    n_cool_df_merge = pd.merge(n_cool_df, non_cool_df, on='technology', how='right')
    n_cool_df_merge.dropna(inplace = True)
    # Input dataframe for non cooling technologies
    # only water withdrawals are being taken
    # Only freshwater supply is assumed for simplicity
    inp_n_cool = (
        make_df(
            "input",
            technology=n_cool_df_merge["technology"],
            value=n_cool_df_merge["value_y"],
            unit="km3/GWa",
            level="water_supply",
            commodity="freshwater_supply",
            time_origin="year",
            mode="M1",
            time="year",
            year_vtg = n_cool_df_merge["year_vtg"].astype(int),
            year_act = n_cool_df_merge["year_act"].astype(int),
            node_loc = n_cool_df_merge["node_loc"],
            node_origin = n_cool_df_merge["node_dest"]
        )
    )

    # append the input data to results
    results["input"] = inp_n_cool

    # # technical lifetime
    # year = info.Y
    # if 2010 in year:
    #     pass
    # else:
    #     year.insert(0, 2010)

    # tl = (
    #     make_df(
    #         "technical_lifetime",
    #         technology=inp_n_cool["technology"].drop_duplicates(),
    #         value=20,
    #         unit="year",
    #     )
    #     .pipe(broadcast, year_vtg=year, node_loc=info.N[1:13])
    #     .pipe(same_node)
    # )
    #
    # results["technical_lifetime"] = tl

    return results
