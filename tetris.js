//Canvasの取得
const canvas=document.getElementById('tetrisCanvas');//html側に書いた捜査対象の画面の特定
const ctx = canvas.getContext('2d');//描画する命令セット,canvas要素のgetContext()メソッドを取得
const block_size=35;

//ミノの形
const shapes=[[[0,0,0,0],[1,1,1,1],[0,0,0,0],[0,0,0,0]],//I
         [[1,0,0,0],[1,1,1,0],[0,0,0,0],[0,0,0,0]],//L
         [[0,0,0,1],[0,1,1,1],[0,0,0,0],[0,0,0,0]],//J
         [[0,0,0,0],[0,1,1,0],[0,1,1,0],[0,0,0,0]],//o
         [[0,1,0,0],[1,1,1,0],[0,0,0,0],[0,0,0,0]],//T
         [[0,0,0,0],[1,1,0,0],[0,1,1,0],[0,0,0,0]],//z
         [[0,0,0,0],[0,1,1,0],[1,1,0,0],[0,0,0,0]]];//s
//色
const colors=["#00ffff", "#ffff00", "#800080", "#00ff00", "#ff0000", "#0000ff", "#ffa500"]

//Blockクラスの作成
class Block{
    
}



//ボードの初期化
let board;
function initData(){
    board=new Array(10);
    for (let step=0;step<10;step++){
        board[step]=new Array(20);
    }
    for (let step=0;step<10;step++){
        for (let step2=0;step2<20;step2++){
            board[step][step2]=0;
        }
    }
    return board;
}
board=initData();

//画面に描画する関数
function draw(){
    //画面クリア
    ctx.fillStyle='black';//塗りつぶしのスタイルを決定
    ctx.fillRect(0,0,canvas.clientWidth,canvas.height);//実際にぬりつぶし, 座標(0,0)に幅canvasclientWidth、高さcanvasheightのblack長方形を描画

    for (let x=0;x<10;x++){
        for (let y=0;y<20;y++){
            if (board[x][y]==1){
                ctx.fillStyle="green";
                ctx.fillRect(x*block_size,y*block_size,block_size-1,block_size-1);
            }
        }
    } 
}
draw()