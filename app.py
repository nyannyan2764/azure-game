import random
import string
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

# ---------------------------------------------------------
# 設定と初期化
# ---------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_change_this_in_production'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# ---------------------------------------------------------
# クイズデータ (ここに追加・変更してください)
# ---------------------------------------------------------
# 'q': 問題文, 'a': 正解の数字(数値)
QUIZ_DATA = [
    {'q': '地球の現在の人口は何人？(億人)', 'a': 80.0},
    {'q': 'エベレストの標高は何メートル？', 'a': 8848},
    {'q': '日本にある都道府県の数は？', 'a': 47},
    {'q': '東京タワーの高さは何メートル？', 'a': 333},
    {'q': '1年は何分？', 'a': 525600},
    {'q': '人間の骨の数は大人で約何本？', 'a': 206},
    {'q': '光が1秒間に進む距離は何万キロメートル？', 'a': 30},
    {'q': 'ピアノの鍵盤の数は？', 'a': 88},
    {'q': '太陽の表面温度は約何千度？', 'a': 6},
    {'q': '月までの距離は約何万キロメートル？', 'a': 38}
]

# ---------------------------------------------------------
# グローバルゲームステート
# ---------------------------------------------------------
class GameState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.players = {}  # {sid: {name, ready, score, role, id}}
        self.settings = {
            'total_turns': 3,
            'error_threshold': 10,  # 誤差n%以上で裏切り者の得点
            'traitor_multiplier': 2  # 裏切り者を外した場合の倍率
        }
        self.status = 'lobby'  # lobby, game, result, vote, final
        self.current_turn = 0
        self.current_question = None
        self.current_answerer_sid = None
        self.game_questions = []
        self.votes = {}
        self.logs = []
        self.traitor_sid = None

    def add_log(self, message, type='info'):
        self.logs.append({'msg': message, 'type': type})
        return self.logs

game = GameState()

# ---------------------------------------------------------
# ルート
# ---------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

# ---------------------------------------------------------
# SocketIO イベント
# ---------------------------------------------------------

@socketio.on('join_game')
def handle_join(data):
    """プレイヤー参加・再接続処理"""
    player_name = data.get('name')
    # 一意なID (ブラウザのlocalStorageに保存されるID)
    client_uid = data.get('uid')
    
    # 既存プレイヤーの再接続チェック
    existing_sid = None
    for sid, p in game.players.items():
        if p.get('uid') == client_uid:
            existing_sid = sid
            break
    
    if existing_sid:
        # 情報を引き継ぎ
        old_data = game.players.pop(existing_sid)
        old_data['connected'] = True
        game.players[request.sid] = old_data
        player_name = old_data['name'] # 名前を復元
    else:
        # 新規参加 (ゲーム中は参加不可にする場合はここで弾くが、要件により途中参加/復帰考慮)
        game.players[request.sid] = {
            'name': player_name,
            'uid': client_uid,
            'ready': False,
            'score': 0,
            'role': 'citizen',
            'connected': True
        }
        game.add_log(f"{player_name} が参加しました。", 'system')

    emit('update_state', _get_client_state(), broadcast=True)
    # 個別に自分のIDを教える
    emit('your_info', {'sid': request.sid, 'uid': client_uid})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in game.players:
        game.players[request.sid]['connected'] = False
        # 完全削除はせず、再接続を待つ
        emit('update_state', _get_client_state(), broadcast=True)

@socketio.on('update_settings')
def handle_settings(data):
    if game.status == 'lobby':
        game.settings.update({
            'total_turns': int(data.get('turns', 3)),
            'error_threshold': float(data.get('threshold', 10)),
            'traitor_multiplier': float(data.get('multiplier', 2))
        })
        game.add_log("ゲーム設定が変更されました。", 'system')
        emit('update_state', _get_client_state(), broadcast=True)

@socketio.on('toggle_ready')
def handle_ready():
    if request.sid in game.players and game.status == 'lobby':
        game.players[request.sid]['ready'] = not game.players[request.sid]['ready']
        emit('update_state', _get_client_state(), broadcast=True)
        
        # 全員準備完了ならスタート
        if all(p['ready'] for p in game.players.values()) and len(game.players) >= 3:
             start_game()

def start_game():
    game.status = 'game'
    game.current_turn = 1
    game.logs = [] # ログリセットあるいは保持（要件によるがここでは見やすくするため保持）
    game.add_log("=== ゲームスタート ===", 'system')
    
    # 役割決定
    sids = list(game.players.keys())
    game.traitor_sid = random.choice(sids)
    for sid in game.players:
        game.players[sid]['role'] = 'traitor' if sid == game.traitor_sid else 'citizen'
        game.players[sid]['score'] = 0
    
    # 問題選定 (重複なし)
    game.game_questions = random.sample(QUIZ_DATA, min(game.settings['total_turns'], len(QUIZ_DATA)))
    
    next_turn()

def next_turn():
    if game.current_turn > len(game.game_questions):
        # 最終ターン終了 -> 投票フェーズへ
        game.status = 'vote'
        game.votes = {}
        game.add_log("全ターン終了！裏切り者投票の時間です。", 'important')
        emit('update_state', _get_client_state(), broadcast=True)
        return

    game.status = 'game'
    game.current_question = game.game_questions[game.current_turn - 1]
    
    # 回答者指名 (ランダム)
    game.current_answerer_sid = random.choice(list(game.players.keys()))
    answerer_name = game.players[game.current_answerer_sid]['name']
    
    game.add_log(f"第{game.current_turn}ターン: 回答者は {answerer_name} です。", 'info')
    
    # 全員に状態送信
    emit('update_state', _get_client_state(), broadcast=True)

@socketio.on('submit_answer')
def handle_answer(data):
    if game.status != 'game' or request.sid != game.current_answerer_sid:
        return

    try:
        user_answer = float(data.get('answer'))
    except ValueError:
        return

    correct = game.current_question['a']
    error_percent = abs(user_answer - correct) / abs(correct) * 100
    
    game.add_log(f"回答: {user_answer} (正解: {correct}) - 誤差: {error_percent:.1f}%", 'result')

    # ポイント計算
    points = 100
    threshold = game.settings['error_threshold']
    
    winner = ""
    if error_percent >= threshold:
        # 誤差が大きい -> 裏切り者の勝利
        game.players[game.traitor_sid]['score'] += points
        winner = "裏切り者"
    else:
        # 誤差が小さい -> 市民の勝利 (全員に加算)
        for sid, p in game.players.items():
            if p['role'] == 'citizen':
                p['score'] += points
        winner = "市民チーム"

    game.last_result = {
        'answer': user_answer,
        'correct': correct,
        'error': error_percent,
        'winner': winner
    }
    
    game.status = 'result' # 結果表示フェーズ
    emit('update_state', _get_client_state(), broadcast=True)

@socketio.on('next_scene')
def handle_next_scene():
    # 誰かが押したら進むのではなく、全員が「次へ」を押したら進む仕様に変更（リクエスト要件）
    # シンプルにするため、ここでは「誰か（あるいはホスト）」が進める形式ではなく、
    # 投票完了ロジックと同様、全員の同意を待つフラグ管理を簡易的に実装します。
    # ここではUXを考慮し、「誰かが押したら進む」ではなく、一時的なReadyフラグを使います。
    
    if request.sid in game.players:
        game.players[request.sid]['round_ready'] = True
    
    # 接続中のプレイヤー全員が準備完了したら
    connected_players = [p for p in game.players.values() if p['connected']]
    if all(p.get('round_ready', False) for p in connected_players):
        # フラグクリア
        for p in game.players.values():
            p['round_ready'] = False
            
        if game.status == 'result':
            game.current_turn += 1
            next_turn()
        elif game.status == 'final':
             # 終了後のリセット待機
            pass

    emit('update_state', _get_client_state(), broadcast=True)


@socketio.on('vote_traitor')
def handle_vote(data):
    if game.status != 'vote':
        return
        
    target_uid = data.get('target_uid')
    # 自分には投票できない (クライアント側でも制御するがサーバーでもチェック)
    if game.players[request.sid]['uid'] == target_uid:
        return

    game.votes[request.sid] = target_uid
    
    # 全員投票完了チェック
    connected_count = len([p for p in game.players.values() if p['connected']])
    if len(game.votes) >= connected_count:
        calc_final_result()

def calc_final_result():
    # 投票集計
    vote_counts = {}
    for vid in game.votes.values():
        vote_counts[vid] = vote_counts.get(vid, 0) + 1
    
    # 最多得票者 (同率の場合は全員対象とする)
    max_votes = 0
    if vote_counts:
        max_votes = max(vote_counts.values())
    
    suspect_uids = [uid for uid, count in vote_counts.items() if count == max_votes]
    
    # 裏切り者のUID特定
    traitor_uid = None
    for p in game.players.values():
        if p['role'] == 'traitor':
            traitor_uid = p['uid']
            break
            
    traitor_caught = traitor_uid in suspect_uids
    
    final_msg = ""
    if traitor_caught:
        # 裏切り者敗北 (問答無用で負け -> スコア0にする、あるいは市民にボーナス等)
        # 要件:「正解なら問答無用で裏切り者は負け」
        # ここでは裏切り者のスコアを0にし、勝者を市民とする
        for sid, p in game.players.items():
            if p['role'] == 'traitor':
                p['score'] = 0
        final_msg = "裏切り者が追放されました！市民の勝利です！"
    else:
        # 裏切り者勝利 -> ポイントn倍
        multiplier = game.settings['traitor_multiplier']
        for sid, p in game.players.items():
            if p['role'] == 'traitor':
                p['score'] *= multiplier
        final_msg = f"裏切り者は逃げ切りました！裏切り者のポイントが{multiplier}倍になります！"

    game.add_log(final_msg, 'important')
    game.status = 'final'
    emit('update_state', _get_client_state(), broadcast=True)

@socketio.on('reset_game')
def handle_reset():
    # 全員がリセットボタンを押したら、という要件
    if request.sid in game.players:
        game.players[request.sid]['reset_ready'] = True
    
    connected_players = [p for p in game.players.values() if p['connected']]
    if all(p.get('reset_ready', False) for p in connected_players):
        game.reset()
        # プレイヤー接続情報は維持しつつ、状態をリセット
        # ※本来はgame.reset()で消えるが、接続中のユーザーを再登録する処理が必要
        # ここでは簡易的に「リロードしてください」を促すか、
        # あるいはGameStateのロジックを調整してPlayerを残す。
        # 修正: GameState.reset()を呼ぶと全員消えるので、接続維持ロジックを入れる
        
        # 現在の接続リストを退避
        current_players = game.players
        game.reset() # 完全リセット
        
        # 接続中ユーザーをLobbyに戻す
        for sid, p in current_players.items():
            if p['connected']:
                game.players[sid] = {
                    'name': p['name'],
                    'uid': p['uid'],
                    'ready': False,
                    'score': 0,
                    'role': 'citizen',
                    'connected': True
                }
        
        game.add_log("ゲームがリセットされました。", 'system')
        emit('update_state', _get_client_state(), broadcast=True)
    else:
        # 状態更新（誰がリセット待ちか表示するため）
        emit('update_state', _get_client_state(), broadcast=True)

@socketio.on('chat_message')
def handle_chat(data):
    msg = data.get('msg')
    # 数字が含まれているか簡易チェックしてログに残す（メモ機能）
    name = game.players[request.sid]['name']
    game.add_log(f"{name}: {msg}", 'chat')
    emit('update_state', _get_client_state(), broadcast=True)

# ---------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------
def _get_client_state():
    """クライアントに送信しても安全なデータを整形"""
    # 裏切り者の正体は隠す
    players_safe = {}
    for sid, p in game.players.items():
        players_safe[sid] = {
            'name': p['name'],
            'ready': p['ready'],
            'score': p['score'],
            'connected': p['connected'],
            'uid': p['uid'],
            'round_ready': p.get('round_ready', False),
            'reset_ready': p.get('reset_ready', False),
            # 役割はゲーム終了まで隠す（ただし自分の役割は別途知っている）
            # Finalフェーズのみ公開
            'role': p['role'] if game.status == 'final' else '?'
        }
        
    return {
        'status': game.status,
        'settings': game.settings,
        'current_turn': game.current_turn,
        'logs': game.logs,
        'players': players_safe,
        'question': game.current_question if game.status in ['game', 'result'] else None,
        'answerer_sid': game.current_answerer_sid,
        'last_result': getattr(game, 'last_result', None),
        'traitor_sid': game.traitor_sid if game.status == 'final' else None # 最後に公開
    }

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=8000)