package bot

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

func (b *Bot) superPanel(ctx context.Context, uid int64, state panel, notice string) error {
	var name string
	var active bool
	var title *string
	err := b.db.QueryRow(ctx, `SELECT c.name,a.cluster_id IS NOT NULL,g.title FROM cpbot.clusters c
		LEFT JOIN cpbot.cluster_authorizations a ON a.cluster_id=c.id LEFT JOIN cpbot.cluster_management_groups g ON g.cluster_id=c.id
		WHERE c.id=$1`, state.cluster).Scan(&name, &active, &title)
	if err != nil {
		return err
	}
	text := "👑 Superadmin 后台\n当前集群：" + name
	choices := [][]choice{{{"选择集群", "super.pick:0"}, {"＋ 新建集群", "super.new"}}, {{"管理后台", "admin"}}}
	if active {
		text += "\n集群授权：已激活"
		choices = append(choices, []choice{{"撤销集群授权", "super.revoke"}})
	} else {
		text += "\n集群授权：未激活"
		choices = append(choices, []choice{{"生成授权码", "super.code"}, {"输入授权码", "super.redeem"}})
	}
	if title != nil {
		text += "\n管理群：" + *title
	}
	if notice != "" {
		text += "\n\n" + notice
	}
	state.input = ""
	return b.show(ctx, uid, state, text, append(choices, []choice{{"返回主菜单", "home"}}))
}

func (b *Bot) superAction(ctx context.Context, uid int64, state panel, kind, arg string) error {
	if !b.admins[uid] {
		return pgstore.UserError("仅 superadmin 可以操作。")
	}
	id, _ := strconv.ParseInt(arg, 10, 64)
	switch kind {
	case "super.open":
		state.cluster = id
		return b.superPanel(ctx, uid, state, "")
	case "super.pick":
		list, err := b.adminClusters(ctx, uid)
		if err != nil {
			return err
		}
		if len(list) == 1 {
			state.cluster = list[0].ID
			return b.superPanel(ctx, uid, state, "")
		}
		page := min(max(int(id), 0), (len(list)-1)/8)
		var rows [][]choice
		for _, c := range list[page*8 : min(len(list), (page+1)*8)] {
			rows = append(rows, []choice{{c.Name, fmt.Sprintf("super.open:%d", c.ID)}})
		}
		rows = append(rows, navigation(page, (page+1)*8 < len(list), "super.pick:")...)
		rows = append(rows, []choice{{"＋ 新建集群", "super.new"}, {"返回主菜单", "home"}})
		return b.show(ctx, uid, state, "👑 选择集群", rows)
	case "super.new":
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if err := tx.QueryRow(ctx, `INSERT INTO cpbot.clusters(name) VALUES('') RETURNING id`).Scan(&state.cluster); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `UPDATE cpbot.clusters SET name=id::text WHERE id=$1`, state.cluster); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "新建集群", state.cluster)
		})
		if err != nil {
			return err
		}
		return b.superPanel(ctx, uid, state, "集群已创建，请生成授权码开通服务。")
	case "super.code":
		var bytes [24]byte
		if _, err := rand.Read(bytes[:]); err != nil {
			return err
		}
		code := base64.RawURLEncoding.EncodeToString(bytes[:])
		digest, _ := activationDigest(code)
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			var authorized bool
			if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.cluster_authorizations WHERE cluster_id=$1)`, state.cluster).Scan(&authorized); err != nil {
				return err
			}
			if authorized {
				return pgstore.UserError("集群已激活。")
			}
			if _, err := tx.Exec(ctx, `UPDATE cpbot.cluster_activation_codes SET revoked=1 WHERE cluster_id=$1 AND redeemed IS NULL AND revoked=0`, state.cluster); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO cpbot.cluster_activation_codes(cluster_id,code_hash,expires)
				VALUES($1,$2,extract(epoch FROM now()+interval '24 hours'))`, state.cluster, digest); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "生成集群授权码", state.cluster)
		})
		if err != nil {
			return err
		}
		return b.superPanel(ctx, uid, state, "授权码只显示一次，24 小时内有效。请私下发给目标群管理者。\n"+code)
	case "super.revoke":
		return b.show(ctx, uid, state, "撤销授权将停止该集群的查档、写评与发布，保留资料和任务。", [][]choice{
			{{"确认撤销", "super.revokeyes"}}, {{"取消", fmt.Sprintf("super.open:%d", state.cluster)}}})
	case "super.revokeyes":
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, `DELETE FROM cpbot.cluster_authorizations WHERE cluster_id=$1`, state.cluster); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `UPDATE cpbot.cluster_activation_codes SET revoked=1 WHERE cluster_id=$1 AND revoked=0`, state.cluster); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "撤销集群授权", state.cluster)
		})
		if err != nil {
			return err
		}
		return b.superPanel(ctx, uid, state, "集群授权已撤销。")
	case "super.redeem":
		return b.promptAdmin(ctx, uid, state, kind, "请输入集群授权码。", fmt.Sprintf("super.open:%d", state.cluster))
	}
	return pgstore.UserError("操作已失效，请返回主菜单。")
}

var codePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{32}$`)

func activationDigest(code string) (string, error) {
	if !codePattern.MatchString(code) {
		return "", pgstore.UserError("请输入完整的集群激活码。")
	}
	digest := sha256.Sum256([]byte(code))
	return hex.EncodeToString(digest[:]), nil
}

func (b *Bot) registerPrompt(ctx context.Context, uid int64, state panel) error {
	return b.promptAdmin(ctx, uid, state, "register.code", "请输入 Superadmin 提供的集群激活码。", "home")
}

func (b *Bot) registrationInput(ctx context.Context, message telegram.Message, state panel) error {
	uid, text := message.From.ID, strings.TrimSpace(message.Text)
	if state.input == "register.group" {
		input, err := chatInput(text)
		if err != nil {
			return err
		}
		chat, _, err := b.validateBinding(ctx, uid, "lookup", input)
		if err != nil {
			return err
		}
		member, err := b.chatMember(ctx, chat.ID, uid)
		if err != nil || !member.admin() {
			return pgstore.UserError("你必须是这个管理群的当前群主或管理员。")
		}
		if err := b.redeem(ctx, uid, state.cluster, state.submitted, &chat); err != nil {
			return err
		}
		return b.adminPanel(ctx, uid, state, "集群已激活，管理群已绑定。")
	}
	digest, err := activationDigest(text)
	if err != nil {
		return err
	}
	if state.input == "super.redeem" {
		if !b.admins[uid] {
			return pgstore.UserError("仅 superadmin 可以操作。")
		}
		if err := b.redeem(ctx, uid, state.cluster, digest, nil); err != nil {
			return err
		}
		return b.superPanel(ctx, uid, state, "集群已激活。")
	}
	err = b.db.QueryRow(ctx, `SELECT cluster_id FROM cpbot.cluster_activation_codes WHERE code_hash=$1 AND redeemed IS NULL
		AND revoked=0 AND expires>extract(epoch FROM now())`, digest).Scan(&state.cluster)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgstore.UserError("此激活码无效，或已过期、使用或作废。")
	}
	if err != nil {
		return err
	}
	state.submitted = digest
	return b.promptAdmin(ctx, uid, state, "register.group", "请将机器人设为管理群管理员，然后发送管理群数字 ID 或 @公开群用户名。", "home")
}

func (b *Bot) redeem(ctx context.Context, uid, cid int64, digest string, chat *telegram.Chat) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		if chat != nil {
			var current *int64
			if err := tx.QueryRow(ctx, `SELECT (SELECT chat_id FROM cpbot.cluster_management_groups WHERE cluster_id=$1)`, cid).Scan(&current); err != nil {
				return err
			}
			if current != nil && *current != chat.ID {
				return pgstore.UserError("请使用原管理群重新激活此集群。")
			}
			if err := claimChat(ctx, tx, cid, chat.ID); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO cpbot.cluster_management_groups(cluster_id,chat_id,title) VALUES($1,$2,$3)
				ON CONFLICT(cluster_id) DO UPDATE SET title=excluded.title`, cid, chat.ID, chat.Title); err != nil {
				return err
			}
		}
		tag, err := tx.Exec(ctx, `UPDATE cpbot.cluster_activation_codes SET redeemed=extract(epoch FROM now())
			WHERE cluster_id=$1 AND code_hash=$2 AND redeemed IS NULL AND revoked=0 AND expires>extract(epoch FROM now())`, cid, digest)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return pgstore.UserError("授权码不适用于当前集群，或已过期、使用、作废。")
		}
		tag, err = tx.Exec(ctx, `INSERT INTO cpbot.cluster_authorizations(cluster_id) VALUES($1) ON CONFLICT DO NOTHING`, cid)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return pgstore.UserError("集群已激活。")
		}
		return logAction(ctx, tx, cid, uid, "激活集群", cid)
	})
}
