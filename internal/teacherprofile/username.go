// Package teacherprofile resolves the public Telegram username from cluster data.
package teacherprofile

// JoinPeople and Username share the same teachers (t) and people (p) aliases.
const JoinPeople = ` LEFT JOIN cpbot.people p ON (p.cluster_id,p.user_id)=(t.cluster_id,t.user_id) `

// An observed username takes precedence. A saved username can fill a gap, but
// must not point to a different known account if the teacher has a numeric ID.
const Username = `coalesce(nullif(p.username,''), CASE WHEN t.user_id IS NULL OR NOT EXISTS (
	SELECT 1 FROM cpbot.people owner WHERE owner.cluster_id=t.cluster_id
	AND lower(owner.username COLLATE "C")=lower(t.username COLLATE "C") AND owner.user_id<>t.user_id
) THEN nullif(t.username,'') END, '')`
