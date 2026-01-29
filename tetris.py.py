import pygame
import numpy as np
import random

Black=(0,0,0)
White=(255,255,255)
MAGENTA=(255,0,255)

Cyan=(0,255,255)
Yellow=(255,255,0)
Purple=(128,0,128)
Green=(0,255,0)
RED=(255,0,0)
Blue=(0,0,255)
Orange=(255,165,0)


block_size=35
ROW=20
WIDTH=10

fall_interval=1000#ミリ秒

Board=np.zeros((20,10))
Board=Board.astype('int')


I_shape=[[0,0,0,0],
         [1,1,1,1],
         [0,0,0,0],
         [0,0,0,0]]
L_shape=[[1,0,0,0],
         [1,1,1,0],
         [0,0,0,0],
         [0,0,0,0]]
J_shape=[[0,0,0,1],
          [0,1,1,1],
          [0,0,0,0],
          [0,0,0,0]]
O_shape=[[0,0,0,0],
          [0,1,1,0],
          [0,1,1,0],
          [0,0,0,0]]
T_shape=[[0,1,0,0],
         [1,1,1,0],
         [0,0,0,0],
         [0,0,0,0]]
S_shape=[[0,0,0,0],
         [1,1,0,0],
         [0,1,1,0],
         [0,0,0,0]]
Z_shape=[[0,0,0,0],
         [0,1,1,0],
         [1,1,0,0],
         [0,0,0,0]]
shape_list=[I_shape,L_shape,J_shape,O_shape,T_shape,S_shape,Z_shape]
color_list=[Cyan,Yellow,Purple,Green,RED,Blue,Orange]

pygame.init()
screen=pygame.display.set_mode((600,800))
clock=pygame.time.Clock()


#これはBlockの「クラス」、Blockとはどういうものかの定義を行う設計図のようなもの
#shapeや位置などの「属性」を持つとともにleftなどの「メソッド（そのインスタンスができる操作、ふるまい）」がある
class Block:
    def __init__(self):#この関数でブロックが形や位置を「属性」として持つことを定義できる
        self.i=random.randrange(0,6)
        self.shape=shape_list[self.i]
        self.color=color_list[self.i]
        self.x=5
        self.y=0

    def rotate(self,Board):
        new_shape=[list(row) for row in zip(*self.shape[::-1])]
        can_move=True
        for i in range(4):
            for j in range(4):
                if new_shape[i][j]==1:
                    next_x=self.x+j
                    next_y=self.y+i
                    if next_y>19 or next_y<0 or next_x<0 or next_x>9:#ブロックが範囲外にないかどうかの確認
                        can_move=False
                    else:
                        if Board[next_y][next_x]==1:#ブロックが衝突していないかの確認
                                can_move=False
        if can_move==True:
            self.shape=new_shape

    
    #メソッドはインスタンスメソッドとクラスメソッドの二つに分けられる、今回はインスタンスメソッドの方で実装する。
    #インスタンスメソッドの場合第一引数にselfを用いる
    def draw(self):
        for i in range(4):
            for j in range(4):
                if self.shape[i][j]==1:
                    pygame.draw.rect(screen,RED,((self.x+j)*block_size,(self.y+i)*block_size,block_size,block_size),0)
    
    def fall(self,Board):
        can_move=True
        for i in range(4):
            for j in range(4):
                if self.shape[i][j]==1:
                    next_x=self.x+j
                    next_y=self.y+i+1
                    if next_y>19 or Board[next_y][next_x]==1:#ブロックが下端に達していないか、衝突していないかの確認
                        can_move=False
                        break
            if can_move==False:
                break
        
        if can_move==False:#これ以上落下できない場合Boardの更新
            for i in range(4):
                    for j in range(4):                        
                        if self.shape[i][j]==1:
                            Board[self.y+i][self.x+j]=1
            return False
        else:
            self.y+=1
            return True
    
    def right(self,Board):
        can_move=True
        for i in range(4):
            for j in range(4):
                if self.shape[i][j]==1:
                    next_x=self.x+j+1
                    next_y=self.y+i
                    if next_x>=10:#ブロックが範囲外かどうかの確認
                        can_move=False
                    else:
                        if Board[next_y][next_x]==1:#ブロックが衝突していないかの確認
                                can_move=False
        if can_move==True:#右移動できるとき
            self.x+=1
            
    def left(self,Board):
        can_move=True
        for i in range(4):
            for j in range(4):
                if self.shape[i][j]==1:
                    next_x=self.x+j-1
                    next_y=self.y+i
                    if next_x<0:#ブロックが範囲外かどうかの確認
                        can_move=False
                    else:
                        if Board[next_y][next_x]==1:#ブロックが衝突していないかの確認
                                can_move=False
        if can_move==True:#右移動できるとき
            self.x-=1
    
    def down(self,Board):
        can_move=True
        for i in range(4):
            for j in range(4):
                if self.shape[i][j]==1:
                    next_x=self.x+j
                    next_y=self.y+i+1
                    if next_y>19:#ブロックが範囲外かどうかの確認
                        can_move=False
                    else:
                        if Board[next_y][next_x]==1:#ブロックが衝突していないかの確認
                                can_move=False
        if can_move==True:#右移動できるとき
            self.y+=1
    
    def bottoms(self,Board):
        can_move=True
        while can_move==True:
            for i in range(4):
                for j in range(4):
                    if self.shape[i][j]==1:
                        next_x=self.x+j
                        next_y=self.y+i+1
                        if next_y>19:#ブロックが範囲外かどうかの確認
                            can_move=False
                        else:
                            if Board[next_y][next_x]==1:#ブロックが衝突していないかの確認
                                    can_move=False
            if can_move==True:#右移動できるとき
                self.y+=1



class Game:
    def __init__(self,screen,Board):
        self.screen=screen
        self.Board=Board
        self.block=Block()
        self.next_block=Block()
        self.game_continue=True
        self.score=0
        self.combo=0
        self.last_fall_time=pygame.time.get_ticks()
        self.now=pygame.time.get_ticks()
        self.turn=0

    def update(self):
        #-----------------ゲームの実行-------------------------
        self.one_block_step()
        #----------------ゲームオーバーの判定---------------------
        for i in range(10):
            if self.Board[0][i]==1:
                self.game_continue=False
        return self.game_continue
    
    def draw(self):
        #-----------------画面に描画----------------------------------
        #画面真っ黒
        self.screen.fill(Black)
        #各マス目の枠線白くする
        for x in range(WIDTH):
            for y in range(ROW):
                #四角形を書く関数pygame.draw.rect() (screenオブジェクト、図形の色、図形の形（左上のｘ座標、左上のｙ座標、横幅、縦幅）)
                pygame.draw.rect(self.screen,White,(x*block_size,y*block_size,block_size,block_size),1)  
        #ブロックが落下しているところは白く塗りつぶす
        for x in range(WIDTH):
            for y in range(ROW):
                if self.Board[y][x]==1:
                    pygame.draw.rect(self.screen,Green,(x*block_size,y*block_size,block_size,block_size),0)  
        #落下中のブロックの描写
        for i in range(4):
            for j in range(4):
                if self.block.shape[i][j]==1:
                    pygame.draw.rect(self.screen,self.block.color,((self.block.x+j)*block_size,(self.block.y+i)*block_size,block_size,block_size),0)
        #スコアの表示
        font=pygame.font.Font(None,55)
        my_score=font.render(f"score:{str(self.score)}",True,MAGENTA)
        self.screen.blit(my_score,[400,100])

        #次に落ちてくるブロックの表示
        shape_size=block_size/1.5
        for i in range(4):
            for j in range(4):
                if self.next_block.shape[i][j]==1:
                    pygame.draw.rect(self.screen,self.next_block.color,((self.next_block.x+j)*shape_size+350,(self.next_block.y+i)*shape_size+300,shape_size,shape_size),0)
        #コンボ数の表示
        my_combo=font.render(f"combo:{str(self.combo)}",True,MAGENTA)
        self.screen.blit(my_combo,[400,200])
        #ゲームオーバーの時は文字出す
        text1=font.render("Game Over!!",True,MAGENTA)
        text2=font.render(f"final score:{str(self.score)}",True,MAGENTA)
        if self.game_continue==False:
            self.screen.fill(Black)
            self.screen.blit(text1,[200,400])
            self.screen.blit(text2,[200,500])

        
    def generate(self):#blockを生成する関数
        self.next_block=Block()#これはBlockインスタンス（ブロックの実体）を作成し、Gameインスタンスの属性として保持
        self.last_fall_time=pygame.time.get_ticks()
        self.now=pygame.time.get_ticks()
        self.turn+=1

    def block_check(self):#ブロックの削除処理
        #-------------------ブロックの更新------------------------
        delete_row=[]#消す行のリスト
        new_Board=[]#新しいBoardの作成

        for i in range (20):
            if np.all(self.Board[i] == 1):
                delete_row.append(i)
            else:
                new_Board.append(self.Board[i].copy())
        while len(new_Board)<20:
            new_Board.insert(0,np.zeros(10,dtype=int))
        #更新
        self.Board[:]=new_Board
        delete_point=len(delete_row)
        if delete_point>0:#行の削除が行われた場合コンボカウント
            self.combo+=1
            self.score+=(self.combo-1)*50
        else:
            self.combo=0
        self.score+=delete_point*100


    #ゲームループの中で毎回この関数が呼ばれると　もし、時間が経ってるならfall　もし、何かキー入力がたまっていたら移動　みたいな動きをする
    def one_block_step(self):
        #--------------ブロックの操作を受け付ける----------
        for event in pygame.event.get():
                if event.type==pygame.KEYDOWN:
                    if event.key==pygame.K_LEFT:
                        self.block.left(self.Board)#この時leftメソッドはgameではなくblockクラスのものなのでself.blockまで書かなきゃダメ
                    elif event.key==pygame.K_RIGHT:
                        self.block.right(self.Board)
                    elif event.key==pygame.K_DOWN:
                        self.block.down(self.Board)
                    elif event.key==pygame.K_0:
                        self.block.bottoms(self.Board)
                    elif event.key==pygame.K_UP:
                        self.block.rotate(self.Board)
        #------------------時間経過によるブロックの落下-----------
        self.now=pygame.time.get_ticks()
        if self.now-self.last_fall_time>fall_interval:
            alive=self.block.fall(self.Board)
            self.last_fall_time=self.now
            if alive==False:#落下完了
                self.block_check()
                self.block=self.next_block#インスタンスの変更
                self.generate()
    """
                #---------------------テトリスAIによる追加部分------------------------------
                best_move=self.chosen_best()
                print(best_move)
                best_x=best_move[0]
                self.block.x=0
                best_num=best_move[1]
                for i in range(best_x):
                    self.block.right(self.Board)
                for i in range(best_num):
                    self.block.rotate(self.Board)
                
    
    
    
    def simulate_drop(self,x,num):#blockを置くx座標と何回転させるかを与える
        #Boardをコピー、ここでもしcopy()を行わずにコピー元直接呼び出すと元のやつも書き換えられちゃう
        now_Board=self.Board.copy()
        #今操作しているブロックのコピーを作成
        now_block_num=self.block.i
        now_block_shape=shape_list[now_block_num]
        now_block_x=x
        now_block_y=0
        now_block=Block()#ここでインスタンス作成
        now_block.shape=now_block_shape
        now_block.x=now_block_x
        now_block.y=now_block_y
        #そもそも今のx座標にブロックを置くことができるのかどうかの判定
        can_put=True
        for i in range(4):
                for j in range(4):
                    if now_block.shape[i][j]==1:
                        next_x=now_block.x+j
                        if next_x<0 or 9<next_x:#ブロックが範囲外かどうかの確認
                            can_put=False
        if can_put==False:
            now_Board[:]=-1
        if can_put==True:
            #now_blockをnum回転させる
            for i in range(num):
                now_block.rotate(now_Board)
            can_move=True
            while can_move==True:
                can_move=now_block.fall(now_Board)
        return now_Board
    
    def evaluate_board(self,evaluate_Board):
        #まず各列の高さを求める
        height_list=[20]*10#各要素は　0=<x<=20 20は何もブロックがないことを示す
        for i in range(10):
            for j in range(20):
                if evaluate_Board[j][i]==1:
                    height_list[i]=j#各列の高さを格納
                    break
        max_height=0#最大高さ、0=<x<=20 0は何もブロックがないことを示す
        for i in range(10):
            temp_height=20-height_list[i]
            if max_height<temp_height:
                max_height=temp_height

        holes=0#上にブロックがある空白マス
        for i in range(10):#各列ごとに見る
            temp_height=height_list[i]#各列の高さ
            if temp_height<20:#temp_height=20の時はブロックなしのため
                for j in range(temp_height,20):
                    if evaluate_Board[j][i]==0:
                        holes+=1

        bunmpiness=0#隣り合う高さの合計
        for i in range(9):
            bunmpiness+=abs(height_list[i]-height_list[i+1])

        delete_line=0
        for i in range (20):
            if np.all(evaluate_Board[i] == 1):
                delete_line+=1

        Total_height=0
        for height in height_list:
            Total_height+=20-height
        
        score=-max_height*5-holes*4-bunmpiness+delete_line*5-Total_height
        return score
    
    def chosen_best(self):
        score=-1000
        best_x=0
        best_num=0
        for i in range(10):
            for j in range(4):
                new_Board=self.simulate_drop(i,j)
                if (new_Board != -1).all():#エラーなく予測を出力できた場合
                    temp_score=self.evaluate_board(new_Board)
                    if score<temp_score:
                        score=temp_score
                        best_x=i
                        best_num=j
        best_move=[]
        best_move.append(best_x)
        best_move.append(best_num)
        return best_move
    """



game_continue=True
game=Game(screen,Board)


while game_continue:#無限ループってこと
    game_continue=game.update()
    game.draw()
    
    pygame.display.flip()#結果の反映
    clock.tick(60)#60fps,１秒間に最大６０回ループを回す
print(game.turn)
pygame.quit()