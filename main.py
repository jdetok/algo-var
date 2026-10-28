# query RORCMPL for packaging sql rules, run each rule's query, compare the results to the results of students with said packaging group
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import os
import sys
import csv

# import oracle library and explicitly set client as thick mode is required
import oracledb
oracledb.init_oracle_client(lib_dir="/Users/dekockjt/oracle/instantclient_23_26")

"""
TODO: build in rule sequencing. if one rule comes first in sequencing, running subsequent rules shouldn't add a student
example: student is in the MR pidms, UG rule calculates they should be UG, that is ignored since they're already in MR

TODO: add CLI options
TODO: add support for aid periods
TODO: bring back support for packaging groups

10/28/2026
clean up script, make it work again for PBB, make PBB rules run for both periods
"""

OUT_DIR = Path(f"./out/algo_var_{datetime.now().strftime('%m%d%y_%H%M%S')}")

# run mode; G for period budget grouping, P for packing groups
MODE = 'G' 

AIDY = '2627'

# bind variables dictionary
BINDS = {'aidy': AIDY}

# packaging group rules to include
INC_RULES = ['MED4L', 'MED3L', 'MED2L', 'MED1L', 'LAWL', 'PROFL', 'GR/PRL']

# period budget rules to include
INC_G_RULES = ['GR', 'UG', 'PL', 'PM', 'MR']

# spriden query for pidms/banner ids
QUERY_SPRIDEN = 'select spriden_pidm, spriden_id from spriden where spriden_change_ind is null and spriden_pidm = :pidm'

# build rorstat query
PHOLD_RORSTAT = ', '.join(f"'{i}'" for i in INC_RULES)
QUERY_RORSTAT = f'''
select rorstat_pidm, rorstat_pgrp_code from rorstat 
where rorstat_pgrp_code in ({PHOLD_RORSTAT}) and rorstat_aidy_code = :aidy
'''

# build rbrapbg (period budget group) query
PHOLD_RBRAPBG = ', '.join(f"'{i}'" for i in INC_G_RULES)
QUERY_RBRAPBG = f'''
select distinct rbrapbg_pidm, rbrapbg_pbgp_code from rbrapbg
where rbrapbg_run_name = 'ACTUAL'
and rbrapbg_aidy_code = :aidy
and rbrapbg_pbgp_code in ({PHOLD_RBRAPBG})
'''

# build rorcmpl (build sql queries) query
QUERY_RORCMPL = f'''
select rorgdat_grp_code, rorgdat_aidy_code, rorgdat_type_ind, rorgdat_slct, rorcmpl_sql_statement
from rorgdat join rorcmpl on rorcmpl_slct = rorgdat_slct
where rorgdat_aidy_code = :aidy and rorgdat_type_ind = '{MODE}'
'''

def connectDB() -> oracledb.Connection:
    load_dotenv() # load DB env vars in .env
    user = os.environ['ORA_USER']
    pswd = os.environ['ORA_PASS']
    port = os.environ['ORA_PORT']
    host = os.environ['ORA_HOST']
    name = os.environ['ORA_NAME']
    dsn = f'{host}:{port}/{name}'
    return oracledb.connect(user=user, password=pswd, dsn=dsn)

def getBIDs(conn: oracledb.Connection, pidms: list[int]) -> dict[int, str]:
    result = {}
    if not pidms:
        return result
    CHUNK = 1000
    with conn.cursor() as cur:
        for i in range(0, len(pidms), CHUNK):
            chunk = pidms[i:i + CHUNK]
            placeholders = ','.join(f':{j}' for j in range(len(chunk)))
            sql = f'''
                select spriden_pidm, spriden_id 
                from spriden where spriden_change_ind is null 
                and spriden_pidm in ({placeholders})
            '''
            cur.execute(sql, chunk)
            for pidm, bid in cur:
                result[pidm] = bid
    return result

def getBIDMap(grp_pidms):
    with connectDB() as conn:
        return getBIDs(conn, list(set().union(*grp_pidms.values())))

def makeCurrentGroupMap():
    print('querying rorstat for current populations...')
    grps_current = defaultdict(set)

    with connectDB() as conn:
        with conn.cursor() as cur:
            cur.execute(QUERY_RORSTAT if MODE == 'P' else QUERY_RBRAPBG, BINDS)
            for pidm, grp in cur:
                if grp in INC_G_RULES:
                    grps_current[grp].add(pidm)

    return dict(grps_current)

def makeRulesMap():
    print('querying rorcmpl for rules...')
    rules = {}
    with connectDB() as conn:
        with conn.cursor() as cur:
            cur.execute(QUERY_RORCMPL, BINDS)
            for row in cur:
                sql_raw = row[4].read() # rorcmpl_sql_statement CLOB as string
        
                # comment out line with pidm bind variable
                lines = []
                for line in sql_raw.splitlines():
                    if ':pidm' in line.lower():
                        lines.append('-- ' + line)
                        continue
                    lines.append(line)
                # map sql rule to its group code
                if row[0] in INC_G_RULES: rules[row[0]] = '\n'.join(lines) # grp code: sql statement
    return rules

def runRule(grp, rule_sql, rorstat_pidms):
    # collect pidms that don't exist in both
    variance = {}
    
    # run the rule from rorcmpl_sql_statement
    rule_pidms = set()
    for period in [f'20{AIDY[2:4]}10', f'20{AIDY[2:4]}20']:
        print(f'+ running {grp} rule for {period}...')
        with connectDB() as conn:
            with conn.cursor() as cur:
                cur.execute(rule_sql, {**BINDS, 'period': period})
                for row in cur:
                    rule_pidms.add(row[0])
    
    # find which students are in the rule result set vs have the rorstat group (vice versa)
    add_pidms = rule_pidms - rorstat_pidms
    remove_pidms = rorstat_pidms - rule_pidms

    # create group variance
    for pidm in add_pidms:
        variance[pidm] = 'add'
    for pidm in remove_pidms:
        variance[pidm] = 'remove'
    
    print(f'  + students with {grp} group code: {len(rorstat_pidms)} | rows from rule: {len(rule_pidms)}')
    return {'var': variance, 'pidms': rule_pidms | rorstat_pidms}

def writeResults(fpath, stus, bid_map, other_pidms):
    with open(fpath, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['bid', 'todo'])
        for pidm, todo in stus.items():
            if pidm in other_pidms:
                continue
            w.writerow([bid_map.get(pidm), todo])

def main():
    # map with pidms for current groups in rorstat
    try:
        grps_current = makeCurrentGroupMap()
    except oracledb.DatabaseError as err:
        print(f'rorstat query failed: {err}')
        sys.exit(1)

    # query rules from rorcmpl
    try:
        rules = makeRulesMap()
    except oracledb.DatabaseError as err:
        print(f'rorcmpl query failed: {err}')
        sys.exit(1)

    # build dicts for the group variances and group pidms
    grp_vars = defaultdict()
    grp_pidms = defaultdict()

    # run the sql rules for the included group codes
    for grp, rule_sql in rules.items():
        try:
            results = runRule(grp, rule_sql, grps_current[grp])
            grp_vars[grp] = results['var']
            grp_pidms[grp] = results['pidms']
        except oracledb.DatabaseError as err:
            print(f'error running rule for {grp}: {err}')

    # get banner IDs from pids
    try:
        bid_map = getBIDMap(grp_pidms)
    except oracledb.DatabaseError as err:
        print(f'error running rule for {grp}: {err}')
        sys.exit(1)

    # create output directory if it doesn't exist
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    
    for name, stus in grp_vars.items():
        fpath = OUT_DIR / f'{name.replace('/', '')}.csv'
        try:
            writeResults(
                fpath, stus, bid_map, 
                set().union(*(pidms for grp, pidms in grp_pidms.items() if grp != name))
            )
        except Exception as err:
            print(f'an error occured attempting to write {fpath}: {err}')

if __name__=="__main__":
    main()