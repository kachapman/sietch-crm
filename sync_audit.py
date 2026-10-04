import psycopg2

def scalar(sql):
    c = psycopg2.connect(host='127.0.0.1', dbname='sietch_crm', user='sietch', password='local_dev_password')
    c.autocommit = True
    cur = c.cursor()
    try:
        cur.execute(sql)
        v = cur.fetchone()
        return v[0] if v else None
    finally:
        c.close()

print("=== Opportunity completeness ===")
c = psycopg2.connect(host='127.0.0.1', dbname='sietch_crm', user='sietch', password='local_dev_password')
c.autocommit = True
cur = c.cursor()
cur.execute("SELECT COUNT(*) total, COUNT(stage_id) stage, COUNT(responsible_user_id) owner, COUNT(actual_close_date) actual_close, COUNT(onlyoffice_id) oo_id, COUNT(expected_close_date) expected_close FROM opportunities")
r = cur.fetchone()
print(f"  total={r[0]} stage={r[1]} owner={r[2]} actual_close={r[3]} oo_id={r[4]} expected_close={r[5]}")

print()
print("=== Per-deal gaps ===")
NO_TAGS = "SELECT COUNT(*) FROM opportunities o WHERE NOT EXISTS (SELECT 1 FROM opportunity_tags t WHERE t.opportunity_id = o.id)"
NO_CF = "SELECT COUNT(*) FROM opportunities o WHERE NOT EXISTS (SELECT 1 FROM opportunity_custom_field_values v WHERE v.opportunity_id = o.id)"
NO_HIST = "SELECT COUNT(*) FROM opportunities o WHERE NOT EXISTS (SELECT 1 FROM history_events h WHERE h.opportunity_id = o.id)"
NO_TASKS = "SELECT COUNT(*) FROM opportunities o WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.opportunity_id = o.id)"
print(f"  no tags: {scalar(NO_TAGS)}")
print(f"  no CF values: {scalar(NO_CF)}")
print(f"  no history events: {scalar(NO_HIST)}")
print(f"  no tasks: {scalar(NO_TASKS)}")

print()
print("=== Cross-table integrity ===")
print(f"  opp_tags -> missing opp:        {scalar('SELECT COUNT(*) FROM opportunity_tags t WHERE NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id = t.opportunity_id)')}")
print(f"  opp_cf -> missing opp:          {scalar('SELECT COUNT(*) FROM opportunity_custom_field_values v WHERE NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id = v.opportunity_id)')}")
print(f"  opp_cf -> missing field:        {scalar('SELECT COUNT(*) FROM opportunity_custom_field_values v WHERE NOT EXISTS (SELECT 1 FROM custom_field_definitions c WHERE c.id = v.field_id)')}")
print(f"  opp_tags -> missing tag:        {scalar('SELECT COUNT(*) FROM opportunity_tags t WHERE NOT EXISTS (SELECT 1 FROM tag_definitions d WHERE d.id = t.tag_id)')}")
print(f"  tasks -> missing opp:           {scalar('SELECT COUNT(*) FROM tasks t WHERE opportunity_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id = t.opportunity_id)')}")
print(f"  hist_attach -> missing event:   {scalar('SELECT COUNT(*) FROM history_attachments a WHERE NOT EXISTS (SELECT 1 FROM history_events e WHERE e.id = a.event_id)')}")
print(f"  hist_attach -> missing opp:     {scalar('SELECT COUNT(*) FROM history_attachments a JOIN history_events e ON e.id = a.event_id WHERE NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id = e.opportunity_id)')}")
print(f"  history_events NULL category:   {scalar('SELECT COUNT(*) FROM history_events WHERE category_id IS NULL')}")
print(f"  history_events NULL author:     {scalar('SELECT COUNT(*) FROM history_events WHERE created_by IS NULL')}")
print(f"  history_events bad author:      {scalar('SELECT COUNT(*) FROM history_events he WHERE created_by IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = he.created_by)')}")
print(f"  proj_doc -> missing opp:        {scalar('SELECT COUNT(*) FROM project_documents p WHERE opportunity_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id = p.opportunity_id)')}")

print()
print("=== Mail Message body coverage ===")
MM_BODY = "SELECT COUNT(*) FROM history_events he JOIN history_categories hc ON hc.id = he.category_id WHERE hc.title = 'Mail Message' AND he.content LIKE '%body_html%'"
MM_TOTAL = "SELECT COUNT(*) FROM history_events he JOIN history_categories hc ON hc.id = he.category_id WHERE hc.title = 'Mail Message'"
MM_CID = "SELECT COUNT(*) FROM history_events he JOIN history_categories hc ON hc.id = he.category_id WHERE hc.title = 'Mail Message' AND he.content LIKE '%cid:%'"
print(f"  Mail Message total: {scalar(MM_TOTAL)}")
print(f"  with body_html:    {scalar(MM_BODY)}")
print(f"  with cid: refs:    {scalar(MM_CID)}")
print(f"  failed fetches:    7,793 (403 — bot lacks folder access; documented in CHANGELOG)")

print()
print("=== Tasks ===")
print(f"  total:    {scalar('SELECT COUNT(*) FROM tasks')}")
print(f"  open:     {scalar('SELECT COUNT(*) FROM tasks WHERE is_closed = FALSE')}")
print(f"  closed:   {scalar('SELECT COUNT(*) FROM tasks WHERE is_closed = TRUE')}")
print(f"  no opp:   {scalar('SELECT COUNT(*) FROM tasks WHERE opportunity_id IS NULL')}")
print(f"  no responsible: {scalar('SELECT COUNT(*) FROM tasks WHERE responsible_user_id IS NULL AND owner_user_id IS NULL')}")

print()
print("=== Disk ===")
import os, subprocess
ha_paths_q = "SELECT file_path FROM history_attachments WHERE file_path IS NOT NULL"
c = psycopg2.connect(host='127.0.0.1', dbname='sietch_crm', user='sietch', password='local_dev_password')
c.autocommit = True
cur = c.cursor()
cur.execute(ha_paths_q)
ha_paths = [r[0] for r in cur.fetchall()]
base = '/home/chapman-server/Projects/new-crm'
present_ha = sum(1 for p in ha_paths if os.path.exists(os.path.join(base, p)))
cur.execute("SELECT file_path FROM project_documents WHERE file_path IS NOT NULL")
pd_paths = [r[0] for r in cur.fetchall()]
present_pd = sum(1 for p in pd_paths if os.path.exists(os.path.join(base, p)))
print(f"  history_attachments: {present_ha}/{len(ha_paths)} bytes on disk")
print(f"  project_documents:   {present_pd}/{len(pd_paths)} bytes on disk")
print(f"  data/history-attachments size: {subprocess.check_output(['du','-sh',f'{base}/data/history-attachments'],text=True).split()[0]}")
print(f"  data/ total size: {subprocess.check_output(['du','-sh',f'{base}/data'],text=True).split()[0]}")