// Package telegram implements the Bot API transport used by cpbot.
package telegram

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type Params map[string]any

type Error struct {
	Code        int
	RetryAfter  int
	Description string
}

func (e *Error) Error() string { return fmt.Sprintf("Telegram API error %d", e.Code) }

type Client struct {
	base string
	http *http.Client
}

func New(token string) *Client {
	return At("https://api.telegram.org/bot" + token)
}

// At also permits using an isolated Bot API endpoint in acceptance tests.
func At(base string) *Client {
	return &Client{base: strings.TrimRight(base, "/"), http: &http.Client{
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (c *Client) Call(ctx context.Context, method string, params Params, result any) error {
	body, err := json.Marshal(params)
	if err != nil {
		return err
	}
	timeout := 10 * time.Second
	if method == "getUpdates" {
		timeout = 40 * time.Second
	}
	return c.request(ctx, method, "application/json", bytes.NewReader(body), timeout, result, true)
}

// CallOnce leaves rate-limit scheduling to the durable publisher queue.
func (c *Client) CallOnce(ctx context.Context, method string, params Params, result any) error {
	body, err := json.Marshal(params)
	if err != nil {
		return err
	}
	timeout := 10 * time.Second
	if method == "getUpdates" {
		timeout = 40 * time.Second
	}
	return c.request(ctx, method, "application/json", bytes.NewReader(body), timeout, result, false)
}

func (c *Client) Photo(ctx context.Context, method string, params Params, filename string, data []byte, result any) error {
	return c.upload(ctx, method, params, "photo", filename, data, result, true)
}

func (c *Client) PhotoOnce(ctx context.Context, method string, params Params, filename string, data []byte, result any) error {
	return c.upload(ctx, method, params, "photo", filename, data, result, false)
}

type Document struct {
	FileID   string `json:"file_id"`
	FileName string `json:"file_name"`
	FileSize int64  `json:"file_size"`
}

func (c *Client) Document(ctx context.Context, chat int64, name string, data []byte) error {
	return c.upload(ctx, "sendDocument", Params{"chat_id": chat}, "document", name, data, nil, true)
}

func (c *Client) DocumentOnce(ctx context.Context, chat int64, name string, data []byte) error {
	return c.upload(ctx, "sendDocument", Params{"chat_id": chat}, "document", name, data, nil, false)
}

func (c *Client) upload(ctx context.Context, method string, params Params, field, filename string, data []byte, result any, retryRateLimit bool) error {
	var body bytes.Buffer
	form := multipart.NewWriter(&body)
	for key, value := range params {
		text, ok := value.(string)
		if !ok {
			encoded, err := json.Marshal(value)
			if err != nil {
				return err
			}
			text = string(encoded)
		}
		if err := form.WriteField(key, text); err != nil {
			return err
		}
	}
	file, err := form.CreateFormFile(field, filename)
	if err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := form.Close(); err != nil {
		return err
	}
	return c.request(ctx, method, form.FormDataContentType(), &body, 60*time.Second, result, retryRateLimit)
}

func (c *Client) request(ctx context.Context, method, contentType string, body io.Reader, timeout time.Duration, result any, retryRateLimit bool) error {
	payload, err := io.ReadAll(body)
	if err != nil {
		return err
	}
	for {
		attemptCtx, cancel := context.WithTimeout(ctx, timeout)
		req, err := http.NewRequestWithContext(attemptCtx, http.MethodPost, c.base+"/"+method, bytes.NewReader(payload))
		if err != nil {
			cancel()
			return &Error{}
		}
		req.Header.Set("Content-Type", contentType)
		resp, err := c.http.Do(req)
		if err != nil {
			cancel()
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return &Error{}
		}
		var envelope struct {
			OK          bool            `json:"ok"`
			Result      json.RawMessage `json:"result"`
			Code        int             `json:"error_code"`
			Description string          `json:"description"`
			Parameters  struct {
				RetryAfter int `json:"retry_after"`
			} `json:"parameters"`
		}
		decodeErr := json.NewDecoder(io.LimitReader(resp.Body, 20<<20)).Decode(&envelope)
		statusCode, retryHeader := resp.StatusCode, resp.Header.Get("Retry-After")
		resp.Body.Close()
		cancel()
		if decodeErr != nil {
			if retryRateLimit && statusCode == http.StatusTooManyRequests {
				retry, _ := strconv.Atoi(retryHeader)
				if err := waitRateLimit(ctx, retry); err != nil {
					return err
				}
				continue
			}
			if statusCode >= 400 {
				return &Error{Code: statusCode}
			}
			return &Error{}
		}
		if !envelope.OK || statusCode != http.StatusOK {
			code := envelope.Code
			if code == 0 && statusCode >= 400 {
				code = statusCode
			}
			retry := envelope.Parameters.RetryAfter
			if retry == 0 {
				retry, _ = strconv.Atoi(retryHeader)
			}
			if retryRateLimit && code == http.StatusTooManyRequests {
				if err := waitRateLimit(ctx, retry); err != nil {
					return err
				}
				continue
			}
			return &Error{Code: code, RetryAfter: retry, Description: envelope.Description}
		}
		if len(envelope.Result) == 0 || (result != nil && json.Unmarshal(envelope.Result, result) != nil) {
			return &Error{}
		}
		return nil
	}
}

func waitRateLimit(ctx context.Context, retryAfter int) error {
	delay := time.Duration(max(1, retryAfter)) * time.Second
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

type Entity struct {
	Type string `json:"type"`
	User *User  `json:"user"`
}

type Message struct {
	Document          *Document `json:"document"`
	Caption           string    `json:"caption"`
	Entities          []Entity  `json:"entities"`
	CaptionEntities   []Entity  `json:"caption_entities"`
	SenderChat        *Chat     `json:"sender_chat"`
	Reply             *Message  `json:"reply_to_message"`
	ForwardFrom       *User     `json:"forward_from"`
	ForwardDate       int64     `json:"forward_date"`
	ForwardSenderName string    `json:"forward_sender_name"`
	NewMembers        []User    `json:"new_chat_members"`
	LeftMember        *User     `json:"left_chat_member"`
	From              User      `json:"from"`
	Text              string    `json:"text"`
	Date              int64     `json:"date"`
	Photos            []Photo   `json:"photo"`
	ID                int64     `json:"message_id"`
	Chat              Chat      `json:"chat"`
	AutomaticForward  bool      `json:"is_automatic_forward"`
	ForwardOrigin     *struct {
		Type       string `json:"type"`
		Sender     *User  `json:"sender_user"`
		SenderName string `json:"sender_user_name"`
		Date       int64  `json:"date"`
		Chat       Chat   `json:"chat"`
		MessageID  int64  `json:"message_id"`
	} `json:"forward_origin"`
	ForwardFromChat      *Chat `json:"forward_from_chat"`
	ForwardFromMessageID int64 `json:"forward_from_message_id"`
}

type Chat struct {
	ID       int64  `json:"id"`
	Type     string `json:"type"`
	Title    string `json:"title"`
	LinkedID int64  `json:"linked_chat_id"`
}

type User struct {
	ID        int64  `json:"id"`
	Username  string `json:"username,omitempty"`
	FirstName string `json:"first_name,omitempty"`
	LastName  string `json:"last_name,omitempty"`
	IsBot     bool   `json:"is_bot,omitempty"`
}

type Photo struct {
	FileID   string `json:"file_id"`
	FileSize int64  `json:"file_size"`
}

type Callback struct {
	ID      string   `json:"id"`
	From    User     `json:"from"`
	Message *Message `json:"message"`
	Data    string   `json:"data"`
}

type ChatMember struct {
	User     User   `json:"user"`
	Status   string `json:"status"`
	IsMember *bool  `json:"is_member"`
}

func (m ChatMember) Present() *bool {
	var value bool
	switch m.Status {
	case "administrator", "creator", "member":
		value = true
	case "restricted":
		return m.IsMember
	case "left", "kicked":
		value = false
	default:
		return nil
	}
	return &value
}

type MemberUpdate struct {
	Chat      Chat       `json:"chat"`
	Date      int64      `json:"date"`
	OldMember ChatMember `json:"old_chat_member"`
	NewMember ChatMember `json:"new_chat_member"`
}

type Update struct {
	ChatMember    *MemberUpdate `json:"chat_member"`
	MyChatMember  *MemberUpdate `json:"my_chat_member"`
	ID            int64         `json:"update_id"`
	Message       *Message      `json:"message"`
	EditedMessage *Message      `json:"edited_message"`
	Callback      *Callback     `json:"callback_query"`
}

func (c *Client) Download(ctx context.Context, fileID string, maxBytes int64) ([]byte, error) {
	var file struct {
		Path string `json:"file_path"`
	}
	if err := c.Call(ctx, "getFile", Params{"file_id": fileID}, &file); err != nil {
		return nil, err
	}
	segments := strings.Split(file.Path, "/")
	for i, part := range segments {
		if part == "" || part == "." || part == ".." || strings.Contains(part, "\\") {
			return nil, fmt.Errorf("Telegram 文件位置无效")
		}
		segments[i] = url.PathEscape(part)
	}
	index := strings.LastIndex(c.base, "/bot")
	if index < 0 {
		return nil, fmt.Errorf("Telegram 文件地址无效")
	}
	endpoint := c.base[:index] + "/file" + c.base[index:] + "/" + strings.Join(segments, "/")
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, &Error{}
	}
	response, err := c.http.Do(req)
	if err != nil {
		return nil, &Error{}
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, &Error{Code: response.StatusCode}
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxBytes+1))
	if err != nil {
		return nil, &Error{}
	}
	if int64(len(data)) > maxBytes {
		return nil, fmt.Errorf("Telegram 文件超过大小限制")
	}
	return data, nil
}
