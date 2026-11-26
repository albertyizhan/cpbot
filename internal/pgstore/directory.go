package pgstore

// BestPublication picks the current thread replica first, with active feeds as fallback.
// The outer query supplies the review alias r.
const BestPublication = `LEFT JOIN LATERAL (
	SELECT p.chat_id,p.message FROM cpbot.publications p
	JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(p.cluster_id,p.binding)
	LEFT JOIN cpbot.posts t ON (t.cluster_id,t.binding,t.teacher)=(p.cluster_id,p.binding,r.teacher)
	WHERE (p.cluster_id,p.review)=(r.cluster_id,r.id) AND b.active=1
	AND ((b.mode='thread' AND p.generation=t.generation) OR (b.mode='feed' AND p.generation=1))
	ORDER BY (b.mode='thread') DESC,b.id LIMIT 1
) pub ON true`
