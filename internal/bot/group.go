package bot

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

func (b *Bot) groupRoute(ctx context.Context, chat int64) (cid int64, target, lookup bool, err error) {
	err = b.db.QueryRow(ctx, `SELECT o.cluster_id,
		EXISTS(SELECT 1 FROM cpbot.membership_targets WHERE (cluster_id,chat_id)=(o.cluster_id,o.chat_id)),
		EXISTS(SELECT 1 FROM cpbot.bindings WHERE (cluster_id,chat_id,mode,active)=(o.cluster_id,o.chat_id,'lookup',1))
		FROM cpbot.chat_ownership o JOIN cpbot.cluster_authorizations a USING(cluster_id) WHERE o.chat_id=$1`, chat).Scan(&cid, &target, &lookup)
	if errors.Is(err, pgx.ErrNoRows) {
		err = nil
	}
	return
}

func (b *Bot) groupMessage(ctx context.Context, msg telegram.Message, edited bool) error {
	forward, err := b.publisher.AutomaticForward(ctx, msg)
	if err != nil || forward {
		return err
	}
	cid, targetGroup, lookup, err := b.groupRoute(ctx, msg.Chat.ID)
	if err != nil || !targetGroup && !lookup {
		return err
	}
	if msg.SenderChat == nil {
		if _, err := b.observe(ctx, cid, msg.From, msg.Date, &msg.Chat, true, nil); err != nil {
			return err
		}
	}
	for _, user := range msg.NewMembers {
		if _, err := b.observe(ctx, cid, user, msg.Date, &msg.Chat, true, &msg.Date); err != nil {
			return err
		}
	}
	if msg.LeftMember != nil {
		if _, err := b.observe(ctx, cid, *msg.LeftMember, msg.Date, &msg.Chat, false, nil); err != nil {
			return err
		}
	}
	if !lookup || edited || msg.SenderChat != nil || msg.From.IsBot {
		return nil
	}
	for _, target := range identityTargets(msg) {
		if target.username == strings.ToLower(b.me.Username) {
			continue
		}
		p, notice, err := b.resolve(ctx, cid, target)
		var userErr pgstore.UserError
		if errors.As(err, &userErr) {
			if err := b.send(ctx, msg.Chat.ID, userErr.Error()); err != nil {
				return err
			}
			continue
		}
		if err != nil {
			return err
		}
		if p.ID == 0 || !b.shouldReply(msg.Chat.ID, p.ID) {
			continue
		}
		if !targetGroup && p.User != nil {
			member, err := b.chatMember(ctx, msg.Chat.ID, *p.User)
			if err == nil && member.presentState() != nil {
				if err := saveMembership(ctx, b.db, cid, p.ID, msg.Chat, *member.presentState(), float64(time.Now().Unix()), nil); err != nil {
					return err
				}
			}
		}
		card, err := b.card(ctx, cid, p, msg.Chat.ID)
		if err != nil {
			return err
		}
		if notice != "" {
			card = notice + "\n\n" + card
		}
		link := fmt.Sprintf("https://t.me/%s?start=c%d_", b.me.Username, cid)
		buttons := [][]telegram.Params{{{"text": "新增标签", "url": fmt.Sprintf("%sedit_%d", link, p.ID)},
			{"text": "删除标签", "url": fmt.Sprintf("%sdelete_%d", link, p.ID)}}}
		if err := b.api.Call(ctx, "sendMessage", telegram.Params{"chat_id": msg.Chat.ID, "text": card + "\n\n以上为查询时的资料，更新后请重新查询。",
			"reply_markup": telegram.Params{"inline_keyboard": buttons}, "reply_parameters": telegram.Params{"message_id": msg.ID, "allow_sending_without_reply": true}}, nil); err != nil {
			return err
		}
	}
	return nil
}

func (b *Bot) shouldReply(chat, person int64) bool {
	now, key := time.Now(), [2]int64{chat, person}
	if now.Before(b.triggers[key]) {
		return false
	}
	if len(b.triggers) >= 2048 {
		for item, expiry := range b.triggers {
			if !now.Before(expiry) {
				delete(b.triggers, item)
			}
		}
		if len(b.triggers) >= 2048 {
			for item := range b.triggers {
				delete(b.triggers, item)
				break
			}
		}
	}
	b.triggers[key] = now.Add(15 * time.Second)
	return true
}

func (b *Bot) chatMemberUpdate(ctx context.Context, e telegram.MemberUpdate) error {
	if e.Chat.Type != "group" && e.Chat.Type != "supergroup" {
		return nil
	}
	cid, target, lookup, err := b.groupRoute(ctx, e.Chat.ID)
	present := e.NewMember.Present()
	if err != nil || !target && !lookup || present == nil {
		return err
	}
	var joined *int64
	if previous := e.OldMember.Present(); *present && previous != nil && !*previous {
		joined = &e.Date
	}
	_, err = b.observe(ctx, cid, e.NewMember.User, e.Date, &e.Chat, *present, joined)
	return err
}

func (b *Bot) myChatMember(ctx context.Context, e telegram.MemberUpdate) error {
	if e.NewMember.Status == "administrator" || e.NewMember.Status == "creator" {
		return nil
	}
	_, err := b.db.Exec(ctx, `UPDATE cpbot.bindings SET active=0,error='机器人管理员权限已失效'
		WHERE chat_id=$1 OR discussion=$1`, e.Chat.ID)
	return err
}
