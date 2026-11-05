"""Use one bounded worker queue; preserve every CID without replay."""
def send(bot,original,aweme_id,comments,digg_type=1,concurrency=8):
    if not comments:return []
    bot._check_stop()
    limit=min(8,max(1,int(concurrency or 8)))
    bot.log(f'[api-paced] {len(comments)} CID; hàng đợi liên tục, tối đa {limit} request song song.')
    rows=list(original(bot,aweme_id,comments,digg_type=digg_type,concurrency=limit))
    return rows[:len(comments)]+[(False,-1,'Thiếu phản hồi CID; chưa xác nhận') for _ in range(max(0,len(comments)-len(rows)))]
def install():
    import tiktok_reply as tt
    cls=tt.TikTokLikeBot
    original=cls.api_digg_comments
    cls.api_digg_comments=lambda self,*a,**k:send(self,original,*a,**k)
