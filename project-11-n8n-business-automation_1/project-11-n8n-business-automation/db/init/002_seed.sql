-- ============================================================================
-- Reference data: routing queues, SLA policy.
-- Synthetic values only - no real TCS data is used anywhere in this project.
-- ============================================================================
SET search_path TO app, public;

INSERT INTO queues (queue_code, display_name, owner_email, escalation_email, description) VALUES
  ('L1_SERVICE_DESK', 'L1 Service Desk',        'l1.desk@tcs-demo.local',    'desk.lead@tcs-demo.local',
   'Password resets, how-to questions, generic software issues.'),
  ('L2_ENDPOINT',     'L2 Endpoint Engineering', 'l2.endpoint@tcs-demo.local','endpoint.lead@tcs-demo.local',
   'Laptop/desktop hardware, OS builds, peripherals, driver faults.'),
  ('NETWORK_OPS',     'Network Operations',      'netops@tcs-demo.local',     'netops.lead@tcs-demo.local',
   'VPN, Wi-Fi, LAN/WAN, DNS, firewall and connectivity faults.'),
  ('IAM_ACCESS',      'Identity & Access Mgmt',  'iam@tcs-demo.local',        'iam.lead@tcs-demo.local',
   'Account provisioning, role/group membership, application entitlements.'),
  ('SECURITY_IR',     'Security Incident Resp.', 'secops@tcs-demo.local',     'ciso.office@tcs-demo.local',
   'Phishing, malware, data loss, suspected compromise - always P1.'),
  ('HUMAN_REVIEW',    'Human Review Desk',       'triage.review@tcs-demo.local','desk.lead@tcs-demo.local',
   'Fallback queue for ambiguous requests or low AI confidence.')
ON CONFLICT (queue_code) DO UPDATE
  SET display_name     = EXCLUDED.display_name,
      owner_email      = EXCLUDED.owner_email,
      escalation_email = EXCLUDED.escalation_email,
      description      = EXCLUDED.description;

INSERT INTO sla_policy (priority, response_mins, resolve_mins, description) VALUES
  ('P1',  15,   240, 'Critical - business stopped, security incident, or VIP outage.'),
  ('P2',  60,   480, 'High - significant degradation, workaround exists.'),
  ('P3', 240,  1440, 'Medium - single user impaired, standard service request.'),
  ('P4', 480,  4320, 'Low - informational, cosmetic, or scheduled work.')
ON CONFLICT (priority) DO UPDATE
  SET response_mins = EXCLUDED.response_mins,
      resolve_mins  = EXCLUDED.resolve_mins,
      description   = EXCLUDED.description;
