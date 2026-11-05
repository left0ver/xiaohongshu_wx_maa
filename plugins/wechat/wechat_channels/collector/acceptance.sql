-- Local SQLite acceptance only. This does not query the deferred server database.
SELECT event_id, dedupe_key, collected_at, status,
       json_extract(event_json, '$.device_id') AS device_id,
       json_extract(event_json, '$.source_mode') AS source_mode,
       json_extract(event_json, '$.payload.authorNickname') AS author_nickname,
       json_extract(event_json, '$.payload.authorDouyinId') AS channel_id,
       json_extract(event_json, '$.payload.description') AS description,
       json_extract(event_json, '$.payload.publishTimeText') AS publish_time_text,
       json_extract(event_json, '$.payload.publishLocation') AS publish_location,
       json_extract(event_json, '$.payload.returnVerified') AS return_verified
FROM video_drafts
ORDER BY collected_at;

-- Both queries must return no rows.
SELECT dedupe_key, count(*) AS copies
FROM video_drafts GROUP BY dedupe_key HAVING count(*) > 1;

SELECT event_id FROM video_drafts
WHERE status <> 'local_only'
   OR coalesce(json_extract(event_json, '$.event_type'), '') <> 'VIDEO'
   OR coalesce(json_extract(event_json, '$.payload.returnVerified'), 0) <> 1
   OR coalesce(json_extract(event_json, '$.payload.authorDouyinId'), '') = ''
   OR coalesce(json_extract(event_json, '$.payload.authorNickname'), '') = ''
   OR coalesce(json_extract(event_json, '$.payload.description'), '') = '';
