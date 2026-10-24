import json, re, sys, statistics as st, datetime as dt
import yt
c = yt.yt()
def dur(s):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', s); h,mi,se=[int(x or 0) for x in m.groups()]; return h*3600+mi*60+se
def channel(handle, maxv=200):
    ch = c.channels().list(part='snippet,statistics,contentDetails', forHandle=handle).execute()['items'][0]
    up = ch['contentDetails']['relatedPlaylists']['uploads']; ids=[]; tok=None
    while len(ids) < maxv:
        r = c.playlistItems().list(part='contentDetails', playlistId=up, maxResults=50, pageToken=tok).execute()
        ids += [i['contentDetails']['videoId'] for i in r['items']]; tok=r.get('nextPageToken')
        if not tok: break
    vids=[]
    for i in range(0,len(ids),50):
        for v in c.videos().list(part='snippet,statistics,contentDetails', id=','.join(ids[i:i+50])).execute()['items']:
            vids.append({'id':v['id'],'title':v['snippet']['title'],'date':v['snippet']['publishedAt'][:10],
              'views':int(v['statistics'].get('viewCount',0)),'likes':int(v['statistics'].get('likeCount',0)),
              'comments':int(v['statistics'].get('commentCount',0)),'sec':dur(v['contentDetails']['duration']),
              'tags':v['snippet'].get('tags',[]),'desc':v['snippet'].get('description','')[:300]})
    s=ch['statistics']
    info={'handle':handle,'title':ch['snippet']['title'],'created':ch['snippet']['publishedAt'][:10],'subs':s.get('subscriberCount'),
          'total_views':s.get('viewCount'),'video_count':s.get('videoCount'),'desc':ch['snippet'].get('description','')[:300],'videos':vids}
    json.dump(info, open(f'research/{handle.strip("@")}.json','w',encoding='utf-8'), ensure_ascii=False, indent=1)
    return info
def report(info, top=15):
    v=info['videos']; print(f"\n### {info['title']} ({info['handle']}) subs={info['subs']} views={info['total_views']} videos={info['video_count']} created={info['created']}")
    print('desc:', info['desc'].replace('\n',' ')[:200])
    if not v: return
    views=[x['views'] for x in v]; print(f"analysed={len(v)} median_views={int(st.median(views))} mean={int(st.mean(views))} median_len={st.median([x['sec'] for x in v])}s")
    dates=sorted(x['date'] for x in v); print('date range', dates[0], '->', dates[-1])
    for x in sorted(v,key=lambda x:-x['views'])[:top]:
        print(f"{x['views']:>11,} {x['sec']:>3}s {x['date']}  {x['title'][:90]}")
if __name__=='__main__':
    for h in sys.argv[1:]: report(channel(h))
