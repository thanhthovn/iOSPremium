// SoundCloud Go+ Unlock Script
var body = $response.body;
var obj = JSON.parse(body);

// Cap nhat thong tin "SoundCloud Go+"
obj.plan = {
    "vendor": "apple",
    "id": "high_tier",
    "manageable": true,
    "plan_upsells": [],
    "plan_id": "go-plus",
    "upsells": [],
    "plan_name": "SoundCloud Go+"
};

// Kich hoat premium
obj.features = [
    {
        "name": "offline_sync",
        "enabled": true,
        "plans": ["mid_tier", "high_tier"]
    },
    {
        "name": "no_audio_ads",
        "enabled": true,
        "plans": ["mid_tier", "high_tier"]
    },
    {
        "name": "hq_audio",
        "enabled": true,
        "plans": ["high_tier"]
    },
    {
        "name": "system_playlist_in_library",
        "enabled": true,
        "plans": []
    },
    {
        "name": "ads_krux",
        "enabled": false,
        "plans": []
    },
    {
        "name": "new_home",
        "enabled": true,
        "plans": []
    },
    {
        "name": "spotlight",
        "enabled": false,
        "plans": []
    },
    {
        "name": "content_reporting",
        "enabled": false,
        "plans": []
    },
    {
        "name": "content_reporting_dsa",
        "enabled": false,
        "plans": []
    }
];

// Chuyen doi và PH
body = JSON.stringify(obj);
$done({ body });
