from collections import defaultdict
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import re
import os
import sys
import csv
import shutil
import zipfile
import argparse
import oracledb # import oracle library and explicitly set client as thick mode is required
oracledb.init_oracle_client(lib_dir="/Users/dekockjt/oracle/instantclient_23_26")

"""
TODO: build in rule sequencing. if one rule comes first in sequencing, running subsequent rules shouldn't add a student
example: student is in the MR pidms, UG rule calculates they should be UG, that is ignored since they're already in MR
TODO: add support for aid periods

10/28/2026
clean up script, make it work again for PBB, make PBB rules run for both periods, added cli options
"""

OUT_ROOT = Path('out')

RUN_MODE_OPTS = {
    'C': {'name': 'output cleanup'},
    'D': {'name': 'dry run'},
    'G': {
        'name': 'Period Budget Groups',
        'abbr': 'pbgp', 
        'include': ['GR', 'UG', 'PL', 'PM', 'MR'], 
        'popl_field': 'rbrapbg_pbgp_code'
    },
    'P': {
        'name': 'Packaging Groups', 
        'abbr': 'pgrp', 
        'include': ['MED4L', 'MED3L', 'MED2L', 'MED1L', 'LAWL', 'PROFL', 'GR/PRL'], 
        'popl_field': 'rorstat_pgrp_code'
    }, 
    'A': {
        'name': 'Default Aid Period', 
        'include': ['OF', 'OFS', 'CADEFS', 'MFS', 'MFS2', 'MFS3', 'MFS4', 'LFS', 'EMIBFS', 'MBAFS', 'GRBFS', 'MADFS', 'PA', 'PA2', 'PA3', 'DPTFS', 'DPT2FS', 'MOTFS', 'MHAFS', 'FAXSPR', 'FXXSPR'],
        'abbr': 'aprd',
        # 'include': ['FXXSPR', 'OF', 'OFL', 'LFS'],
        'popl_field': 'rorstat_aprd_code'
    }
}
DEFAULT_MODE = 'G' 
DEFAULT_AIDY = '2627'
BINDS = {'aidy': DEFAULT_AIDY}

def getOutDirPath(run_mode):
    return OUT_ROOT / f"{RUN_MODE_OPTS[run_mode]['abbr']}_var_{datetime.now().strftime('%m%d%y_%H%M%S')}"

# CLI arguments
def getParsedArgs():
    parser = argparse.ArgumentParser(
        prog='algo-var',
        description='query and run SQL rules from banner to create variances against current populations'
    )
    parser.add_argument('-m', '--mode', default=DEFAULT_MODE)
    parser.add_argument('-a', '--aidy', default=DEFAULT_AIDY)
    parser.add_argument('-d', '--dry', action='store_true')
    parser.add_argument('-c', '--cleanup', action='store_true')
    parser.add_argument('-y', '--bypass', action='store_true')
    parser.add_argument('-q', '--quick', action='store_true')
    parser.add_argument('-z', '--zip', action='store_true')
    return parser.parse_args()

# string of include values for a sql in clause
def INCLUDE_PHOLDERS(run_mode): return ', '.join(f"'{s}'" for s in RUN_MODE_OPTS[run_mode]['include'])

# SQL queries for current populations and for rules
def QUERY_RORSTAT(run_mode='P'): return f'''
select rorstat_pidm, {RUN_MODE_OPTS[run_mode]['popl_field']} from rorstat 
where {RUN_MODE_OPTS[run_mode]['popl_field']} in ({INCLUDE_PHOLDERS(run_mode)}) and rorstat_aidy_code = :aidy
'''
def QUERY_RBRAPBG(run_mode='G'): return f'''
select distinct rbrapbg_pidm, rbrapbg_pbgp_code from rbrapbg
where rbrapbg_run_name = 'ACTUAL' and rbrapbg_aidy_code = :aidy
and rbrapbg_pbgp_code in ({INCLUDE_PHOLDERS(run_mode)})
'''
def QUERY_RORGDAT(run_mode): return f'''
select rorgdat_grp_code, rorgdat_aidy_code, rorgdat_type_ind, rorgdat_slct, rorcmpl_sql_statement
from rorgdat join rorcmpl on rorcmpl_slct = rorgdat_slct
where rorgdat_aidy_code = :aidy and rorgdat_type_ind = '{run_mode}'
'''
def QUERY_RORALGO(): return f'''
select roralgo_aidy_code, roralgo_process, roralgo_algo_code, roralgo_seq_no, roralgo_sql_statement from roralgo 
where roralgo_aidy_code = :aidy and roralgo_process = 'DAPRD' order by roralgo_seq_no
'''
# build population/rules queries based on run mode
def queryPopulation(run_mode):
    if run_mode in ['A', 'P']: return QUERY_RORSTAT(run_mode)
    if run_mode == 'G': return QUERY_RBRAPBG(run_mode)
def queryRules(run_mode):
    if run_mode in ['G', 'P']: return QUERY_RORGDAT(run_mode)
    if run_mode == 'A': return QUERY_RORALGO()

# cli helper returns user y/n input
def askToConfirm(msg):
    while True:
        choice = input(msg).strip().lower()
        if choice in ['y', 'yes']: return True
        if choice in ['n', 'no']: return False
        print("invalid input, enter 'y' or 'n'")

def countSubdirs(dir: Path, include_zip=True):
    if include_zip: 
        return sum(1 for item in dir.iterdir() if item.is_dir() or item.suffix == '.zip')
    return sum(1 for item in dir.iterdir() if item.is_dir())
    
def countFiles(dir: Path):
    return sum(1 for item in dir.iterdir() if (item.is_file() or item.is_symlink()))

# remove all the output subdirs
def cleanupOutDir(dir: Path, confirm=True):
    if dir == '/':
        print(f'- path cannot be system root')
        return

    num_subdirs_start = countSubdirs(dir)

    if confirm:
        msg = f'  * are you sure you want to remove {num_subdirs_start} output directories from ./{dir}? (y/n): '
        if not askToConfirm(msg): 
            print(f'- user cancelled cleanup')
            return
    
    print(f'  + attempting to remove {num_subdirs_start} directories...')
    if dir.is_dir():
        for subdir in dir.iterdir():
            try:
                isdir = subdir.is_dir()
                if isdir or subdir.suffix == '.zip': # won't remove non subdirs
                    rm = True
                    if isdir:
                        for file in subdir.iterdir():
                            if not ((file.is_file() or file.is_symlink) and file.suffix == '.csv'):
                                rm = False
                    if rm:
                        print(f'    + removing {f"{countFiles(subdir)} files in " if isdir else ""}{subdir}...')
                        if isdir: shutil.rmtree(subdir)
                        else: subdir.unlink()
                    else: print(f'    - {subdir} does not contain only .csv files, skipping')
                else: print(f'    - {subdir} is not a directory or .zip file, skipping')
            except OSError as err: print(f'error removing subdir {subdir}: {err}')
        print(f'+ {num_subdirs_start - countSubdirs(dir)} directories removed')
    else: raise ValueError(f'{dir} is not a directory')

def aidyValid(aidy: str):
    clean = aidy.strip()
    if int(clean[0:2]) != (int(clean[2:4]) - 1):
        return False
    return True

def connectDB() -> oracledb.Connection:
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

def makeCurrentGroupMap(run_mode):
    print(f'+ querying {"rbrabpg" if run_mode == 'G' else "rorstat"} for current populations...')
    grps_current = defaultdict(set)

    with connectDB() as conn:
        with conn.cursor() as cur:
            cur.execute(queryPopulation(run_mode), BINDS)
            for pidm, grp in cur:
                if grp in RUN_MODE_OPTS[run_mode]['include']:
                    grps_current[grp].add(pidm)

    return dict(grps_current)

def getRegexMatch(pattern, s): return re.compile(pattern, re.I | re.S).search(s)

# the aid period rules don't select a pidm, we have to add it
def updateAidPeriodRule(sql):
    new = ''
    tables_without_pidms = ['dual', 'robinst']
    if getRegexMatch(r'\bselect\b(?:(?!_pidm\b).)*?\bfrom\b', sql):
        m_tables = getRegexMatch(r'\bfrom\b(.*?)\bwhere\b', sql)
        tables = [' '.join(t.lower().split()) for t in m_tables.group(1).split(',') if t.strip()] if m_tables else None
        
        pidm_table = None
        with_alias = None
        for t in tables:
            parts = t.split()
            table = parts[0]
            alias = parts[-1] if len(parts) > 1 else None

            if table in tables_without_pidms:
                continue

            pidm_table = table
            with_alias = alias
            break

        # get the selected column(s)
        m_selects = getRegexMatch(r'\bselect\b(.*?)\bfrom\b', sql)
        select_arr = [' '.join(s.split()) for s in m_selects.group(1).split(',') if s.strip()] if m_selects else None
        rule_code = select_arr[0].replace("'", "")
        # add the _pidm field from the pidm_table to the selects array
        select_arr.insert(0, f'{f"{with_alias}." if with_alias else ""}{pidm_table}_pidm')

        # comma separated select list to drop in
        selects = ', '.join(select_arr)

        new = f'{sql[:m_selects.start(1)]} {selects} {sql[m_selects.end(1):]}'

        # print(f'{tables} | {pidm_table} | {with_alias} | {selects}')

    return ( rule_code, new )

def makeRulesMap(run_mode, quick=False):
    print(f'+ querying {"roralgo" if run_mode == 'A' else "rorcmpl"} for rules...')
    rules = {}
    with connectDB() as conn:
        with conn.cursor() as cur:
            cur.execute(queryRules(run_mode), BINDS)
            cnt = 0
            for row in cur:
                cnt += 1
                if quick and len(rules) >= 2: break

                sql_raw = row[4].read() # rorcmpl_sql_statement CLOB as string
                sql = sql_raw
                rule_code = row[0]

                # have to add a pidm field to the aid period sequences
                if run_mode == 'A':
                    rule_code, sql = updateAidPeriodRule(sql_raw)
                    
                # comment out line with pidm bind variable
                lines = []
                for line in sql.splitlines():
                    if ':pidm' in line.lower():
                        lines.append('-- ' + line)
                        continue
                    lines.append(line)

                # add all rules or only included for others
                include = RUN_MODE_OPTS[run_mode]['include']
                if not include or len(include) == 0 or rule_code in include: 
                    rules[rule_code] = '\n'.join(lines) # grp code: sql statement
    return rules

def runRule(run_mode, grp, rule_sql, current_pidms):
    # collect pidms that don't exist in both
    variance = {}
    
    # collect pidms returned from the rule
    rule_pidms = set()

    # run for fall and spring periods in aid year for period budget mode
    periods = [f'20{BINDS['aidy'][2:4]}10', f'20{BINDS['aidy'][2:4]}20'] if run_mode == 'G' else ['na']
    for period in periods:
        print(f'  + running {grp} rule{f" for {period}" if run_mode == 'G' else ""}...')
        with connectDB() as conn:
            with conn.cursor() as cur:
                cur.execute(rule_sql, {**BINDS, 'period': period} if run_mode == 'G' else BINDS)
                for row in cur:
                    if not isinstance(row[0], (int, float)):
                        raise ValueError(f'rule {grp} returned non-numeric first column: {row[0]}')
                    rule_pidms.add(row[0])
    
    # find which students are in the rule result set vs have the rorstat group (vice versa)
    add_pidms = rule_pidms - current_pidms
    remove_pidms = current_pidms - rule_pidms

    # create group variance
    for pidm in add_pidms:
        variance[pidm] = 'add'
    for pidm in remove_pidms:
        variance[pidm] = 'remove'
    
    print(f' + students with {grp} group code: {len(current_pidms)} | rows from rule: {len(rule_pidms)}')
    return {'var': variance, 'pidms': rule_pidms | current_pidms}

def writeVarFile(fpath, stus, bid_map, other_pidms):
    with open(fpath, 'w', newline='') as f:
        print(f'  + writing {fpath}')
        w = csv.writer(f)
        w.writerow(['bid', 'todo'])
        for pidm, todo in stus.items():
            if pidm in other_pidms:
                continue
            w.writerow([bid_map.get(pidm), todo])

def writeResults(grp_vars, grp_pidms, bid_map, dir: Path):
    print(f'+ writing variance files...')

    # create output directory if it doesn't exist
    dir.mkdir(parents=True, exist_ok=True)

    for name, stus in grp_vars.items():
        fpath = dir / f'{name.lower().replace('/', '')}.csv'
        try:
            writeVarFile(
                fpath, stus, bid_map, 
                set().union(*(pidms for grp, pidms in grp_pidms.items() if grp != name))
            )
        except Exception as err:
            print(f'- an error occured attempting to write {fpath}: {err}')

def zipResults(dir: Path):
    zpath = dir.with_suffix('.zip')
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(dir.glob('*.csv')):
            zf.write(f, arcname=f.name)
    return zpath

def main():
    load_dotenv()

    # parse CLI args
    args = getParsedArgs()

    # check that a valid aid year was passed
    if not aidyValid(args.aidy):
        print(f'- aid year "{args.aidy}" is not valid')
        sys.exit(1)
    # update aidy bind variable
    BINDS['aidy'] = args.aidy
    
    # set run mode
    if args.mode not in RUN_MODE_OPTS.keys():
        print(f'- run mode must be one of the following: {RUN_MODE_OPTS.keys()}')
        sys.exit(1)
    run_mode = args.mode
    print(f'+ beginning with run mode {run_mode}: {RUN_MODE_OPTS[run_mode]['name']}')

    # quit early for dry run
    if args.dry or run_mode == 'D': 
        print(f'+ executing dry run. args: {args}')
        sys.exit(0)

    # cleanup mode to cleanup the output directory
    if args.cleanup or run_mode == 'C':
        cleanupOutDir(OUT_ROOT, not args.bypass)
        sys.exit(0)

    # map with pidms for current groups in rorstat
    try:
        grps_current = makeCurrentGroupMap(run_mode)
    except oracledb.DatabaseError as err:
        print(f'- rorstat query failed: {err}')
        sys.exit(1)

    # query rules from rorcmpl
    try:
        rules = makeRulesMap(run_mode, args.quick)
    except oracledb.DatabaseError as err:
        print(f'- rorcmpl query failed: {err}')
        sys.exit(1)

    # build dicts for the group variances and group pidms
    grp_vars = defaultdict()
    grp_pidms = defaultdict()

    # run the sql rules for the included group codes
    for grp, rule_sql in rules.items():
        try:
            results = runRule(run_mode, grp, rule_sql, grps_current.get(grp, set()))
            grp_vars[grp] = results['var']
            grp_pidms[grp] = results['pidms']
        except oracledb.DatabaseError as err:
            print(f'- error running rule for {grp}: {err}')

    # get banner IDs from pids
    try:
        bid_map = getBIDMap(grp_pidms)
    except oracledb.DatabaseError as err:
        print(f'- error running rule for {grp}: {err}')
        sys.exit(1)

    # write output files
    out_dir = getOutDirPath(run_mode)
    try:
        writeResults(grp_vars, grp_pidms, bid_map, out_dir)
    except Exception as err:
        print(f'- an error occured attempting to write variance files: {err}')
        sys.exit(1)

    if args.zip:
        print(f'+ wrote {zipResults(out_dir)}')

    print(f'+ complete!')

if __name__=="__main__": 
    main()