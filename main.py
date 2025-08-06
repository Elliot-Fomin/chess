import http.server
import random
import socketserver
import threading
import json
from queue import Queue, Empty
import urllib.request
import urllib.parse
import socket # To get local IP if hosting
import platform
import requests
from requests import Session
import time
import requests.exceptions
import sys
from copy import deepcopy
import pygame
import os

Pieces = ["P", "R", "N", "B", "Q", "K"]
Colors = ["W", "B"]
size = 600/8
move_queue = Queue() # Queue to pass moves from server thread to main thread
client_connected_event = threading.Event() # Event to signal client connection
play_again = threading.Event()
resign = threading.Event()
draw = threading.Event()
opponent_ip = None
server_ip = None
is_host = None # True if hosting, False if joining
my_turn = None
# Store temporary move data during promotion selection
pending_promotion_move = None
http_server = None # To hold the server instance
move_sound = None # Variable to store move sound
capture_sound = None # Variable to store capture sound
opponent_move_queue = Queue()
offer_queue = Queue()
player_status_queue = Queue()
poller_stop_event = threading.Event()
sender_stop_event = threading.Event()
send_queue = Queue()
polling_thread = None
sender_thread = None
last_offer_handled = None # To track offers processed by main thread

def resource_path(relative_path):
    try:
        # If bundled by PyInstaller
        base_path = sys._MEIPASS
    except AttributeError:
        # If running normally
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


class MoveRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        global move_queue
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        try:
            data = json.loads(post_data.decode('utf-8'))
            print(f"Received data: {data}") # Debugging

            # Check if it's a move or a connection ping
            if data.get('type') == 'connect':
                print(f"Client connected from {self.client_address[0]}")
                # --- Store opponent IP on Host ---
                global opponent_ip, is_host
                if is_host and opponent_ip is None: # Only set if we are host and IP isn't already set
                    opponent_ip = self.client_address[0]
                    print(f"Opponent IP automatically set to: {opponent_ip}")
                # ----------------------------------
                # Signal the main thread that the client has connected
                client_connected_event.set()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Connection acknowledged')
            elif data.get('type') == 'play_again':
                print(f"Play again received from {self.client_address[0]}")
                play_again.set()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Play again acknowledged')
            elif data.get('type') == 'resign':
                print(f"Resign received from {self.client_address[0]}")
                resign.set()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Resign acknowledged')
            elif data.get('type') == 'draw':
                print(f"Draw received from {self.client_address[0]}")
                draw.set()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Draw acknowledged')
                
            elif data.get('from_x') is not None: # Assume it's a move if core keys exist
                 # Add the received move data to the queue for the main thread
                move_queue.put(data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Move received')
            else:
                 print("Received unrecognized data format.")
                 self.send_response(400)
                 self.end_headers()
                 self.wfile.write(b'Unrecognized data')

        except json.JSONDecodeError:
            print("Error decoding received data.")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Invalid JSON')
        except Exception as e:
            print(f"Error processing request: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'Server error')

def start_server(port=8000):
    handler = MoveRequestHandler
    # Use 0.0.0.0 to listen on all available interfaces
    try:
        httpd = socketserver.TCPServer(("0.0.0.0", port), handler)
        print(f"Hosting on port {port}")
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True # Allows program to exit even if thread is running
        server_thread.start()
        return httpd # Return server instance if needed later (e.g., for shutdown)
    except OSError as e:
        print(f"Error starting server on port {port}: {e}")
        print("Is another instance running or is the port busy?")
        return None


def send_move(target_ip, port, move_data):
    if not target_ip:
        print("Error: Opponent IP not set.")
        return
    try:
        url = f"http://{target_ip}:{port}"
        data = json.dumps(move_data).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response: # Increased timeout
             print(f"Move sent to {url}, response: {response.status}")
    except urllib.error.URLError as e:
        print(f"Error sending move to {target_ip}:{port}. Is the opponent's server running? Error: {e}")
        # Consider adding logic here to handle connection failures (e.g., notify user, game over?)
    except Exception as e:
        print(f"Unexpected error sending move to {target_ip}:{port}: {e}")


def send_connection_ping(target_ip, port):
    """Sends a simple message to the host to indicate connection."""
    if not target_ip:
        print("Error: Target IP not set for connection ping.")
        return False
    try:
        url = f"http://{target_ip}:{port}"
        data = json.dumps({'type': 'connect'}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as response: # Shorter timeout for ping
             print(f"Connection ping sent to {url}, response: {response.status}")
             return True # Indicate success
    except urllib.error.URLError as e:
        print(f"Error sending connection ping to {target_ip}:{port}. Is the host server running? Error: {e}")
        return False # Indicate failure
    except Exception as e:
        print(f"Unexpected error sending connection ping to {target_ip}:{port}: {e}")
        return False # Indicate failure

def send_offer(target_ip, port, type, color):
    """Sends a simple message to the host to indicate connection."""
    if not target_ip:
        print("Error: Target IP not set for offer.")
        return False
    try:
        url = f"http://{target_ip}:{port}"
        data = json.dumps({'type': type, 'color': color}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as response: # Shorter timeout for ping
             print(f"Offer sent to {url}, response: {response.status}")
             return True # Indicate success
    except urllib.error.URLError as e:
        print(f"Error sending offer to {target_ip}:{port}. Is the host server running? Error: {e}")
        return False # Indicate failure
    except Exception as e:
        print(f"Unexpected error sending offer to {target_ip}:{port}: {e}")
        return False # Indicate failure


class Piece:
    def __init__(self, x, y, color, piece):
        self.x = x
        self.y = y
        self.color = color
        self.piece = piece
        self.en_passantable = False
        if self.piece == "K":
            self.castleable_k = True
            self.castleable_q = True
    def move(self, x, y,board):
        # Store original position for potential promotion/sending move data
        self.original_x, self.original_y = self.x, self.y

        if not self.is_valid_move(x, y,board):
            return False
        
        if self.piece == "K" and abs(x-self.x) <= 1 and abs(y-self.y) <= 1:
            self.castleable_k = False
            self.castleable_q = False
        if self.piece == "R":
            if self.color == "W":
                king = board.piece_at(board.piece_pos("K", "W")[0], board.piece_pos("K", "W")[1])
            else:
                king = board.piece_at(board.piece_pos("K", "B")[0], board.piece_pos("K", "B")[1])
            if self.x == 0 and self.y == king.y:
                king.castleable_q = False
            if self.x == 7 and self.y == king.y:
                king.castleable_k = False
        
        if self.piece == "K" and abs(x-self.x) == 2:
            if x > self.x:
                board.piece_at(x+1, self.y).move(x-1, self.y, board)
            else:
                board.piece_at(x-2, self.y).move(x+1, self.y, board)
            self.castleable_k = False
            self.castleable_q = False
        
        if self.piece == "P" and abs(y-self.y) == 1 and abs(x-self.x) == 1 and board.piece_at(x, y) == None and board.piece_at(x, self.y) != None:
            board.captured_pieces.append(board.piece_at(x, self.y))
            board.pieces.remove(board.piece_at(x, self.y))
        
        if board.piece_at(x, y) != None:
            board.captured_pieces.append(board.piece_at(x, y))
            board.pieces.remove(board.piece_at(x, y))
        
        for piece in board.pieces:
                piece.en_passantable = False
        if self.piece == "P" and abs(y-self.y) == 2:
            self.en_passantable = True

        
        self.x = x
        self.y = y
        return True
    

    def is_valid_move(self, x, y,board):

        if board.piece_at(x, y) != None:
            if board.piece_at(x, y).color == self.color:
                return False
        if self.piece == "P":
            if self.color == "B":
                if self.y == 1 and y == 3 and x == self.x and board.piece_at(x, 2) == None and board.piece_at(x,y) == None:
                    return True
                if y == self.y + 1 and x == self.x and board.piece_at(x, y) == None:
                    return True
                if y == self.y+1 and abs(x-self.x) == 1 and board.piece_at(x, y) != None:
                    return True
                if board.piece_at(x, self.y) !=None:
                    if abs(x-self.x) == 1 and y == self.y+1 and board.piece_at(x, self.y).en_passantable:
                        return True

            if self.color == "W":
                if self.y == 6 and y == 4 and x == self.x and board.piece_at(x, 5) == None and board.piece_at(x,y) == None:
                    return True
                if y == self.y - 1 and x == self.x and board.piece_at(x, y) == None:
                    return True
                if y == self.y-1 and abs(x-self.x) == 1 and board.piece_at(x, y) != None:
                    return True
                if board.piece_at(x, self.y) !=None:
                    if abs(x-self.x) == 1 and y == self.y-1 and board.piece_at(x, self.y).en_passantable:
                        return True
        if self.piece == "R":
            if self.x == x:  # Moving vertically
                step = 1 if y > self.y else -1
                for check_y in range(self.y + step, y, step):
                    if board.piece_at(x, check_y) is not None:
                        return False
                return True
            elif self.y == y:  # Moving horizontally 
                step = 1 if x > self.x else -1
                for check_x in range(self.x + step, x, step):
                    if board.piece_at(check_x, self.y) is not None:
                        return False
                return True
        if self.piece == "B":
            if self.x -x == self.y - y:
                step = 1 if x > self.x else -1
                for check_x in range(self.x + step, x, step):
                    if board.piece_at(check_x, self.y + (check_x - self.x)) is not None:
                        return False
                return True
            elif self.x - x == y - self.y:
                step = 1 if x > self.x else -1
                for check_x in range(self.x + step, x, step):
                    if board.piece_at(check_x, self.y - (check_x - self.x)) is not None:
                        return False
                return True
        if self.piece == "N":
            if abs(self.x-x) == 2 and abs(self.y-y) == 1:
                return True
            elif abs(self.x-x) == 1 and abs(self.y-y) == 2:
                return True
        if self.piece == "K":
            if abs(self.x-x) <= 1 and abs(self.y-y) <= 1:
                return True
            if self.castleable_k and self.y == y and x == self.x+2:
                step = 1
                for check_x in range(self.x+1, x, step):
                    if board.piece_at(check_x, self.y) is not None:
                        return False
                return True
            if self.castleable_q and self.y == y and x == self.x-2:
                step = -1
                for check_x in range(self.x-1, x, step):
                    if board.piece_at(check_x, self.y) is not None:
                        return False
                return True
            
        if self.piece == "Q":
            if self.x -x == self.y - y:
                step = 1 if x > self.x else -1
                for check_x in range(self.x + step, x, step):
                    if board.piece_at(check_x, self.y + (check_x - self.x)) is not None:
                        return False
                return True
            elif self.x - x == y - self.y:
                step = 1 if x > self.x else -1
                for check_x in range(self.x + step, x, step):
                    if board.piece_at(check_x, self.y - (check_x - self.x)) is not None:
                        return False
                return True
            elif self.x == x:  # Moving vertically
                step = 1 if y > self.y else -1
                for check_y in range(self.y + step, y, step):
                    if board.piece_at(x, check_y) is not None:
                        return False
                return True
            elif self.y == y:  # Moving horizontally 
                step = 1 if x > self.x else -1
                for check_x in range(self.x + step, x, step):
                    if board.piece_at(check_x, self.y) is not None:
                        return False
                return True
            
        return False
    
    def draw(self, screen, player_color):
        piece_size = 600/16
        image_name = f"{self.color}{self.piece}.svg"
        # The 'assets' folder is directly in the project root
        image_path = resource_path(f"assets/{image_name}")
        try:
            piece_image = pygame.image.load(image_path)
            # Scale the image to fit the square size using smoothscale for better quality
            if platform.system() == 'Darwin': # macOS
                    target_size = (int(size), int(size))
            else: # Windows or other OS
                target_size = (200, 200)
            piece_image = pygame.transform.smoothscale(piece_image, target_size)
            # Calculate pixel position: board offset (100, 0) + grid position * size
            c,r = self.x, self.y
            if player_color == "B":
                c = 7 - c
                r = 7 - r
            draw_x = 100 + c * size
            draw_y = r * size
            
            screen.blit(piece_image, (draw_x, draw_y))
        except pygame.error as e:
            print(f"Error loading image {image_path}: {e}")
            # Fallback: draw a colored rectangle if image fails
            fallback_color = (255, 0, 0) if self.color == 'W' else (0, 0, 255)
            pygame.draw.rect(screen, fallback_color, (100 + self.x * size, self.y * size, size, size))

    def __eq__(self, other):
        if not isinstance(other, Piece):
            return False
        return self.x == other.x and self.y == other.y and self.color == other.color and self.piece == other.piece

class Board:
    def __init__(self):
        self.pieces = []
        self.side_menu = SideMenu()
        self.captured_pieces = []
        # White pieces
        for i in range(8):
            self.pieces.append(Piece(i, 1, "B", "P"))
        self.pieces.append(Piece(0, 0, "B", "R"))
        self.pieces.append(Piece(7, 0, "B", "R"))
        self.pieces.append(Piece(1, 0, "B", "N"))
        self.pieces.append(Piece(6, 0, "B", "N"))
        self.pieces.append(Piece(2, 0, "B", "B"))
        self.pieces.append(Piece(5, 0, "B", "B"))
        self.pieces.append(Piece(3, 0, "B", "Q"))
        self.pieces.append(Piece(4, 0, "B", "K"))
        # Black pieces
        for i in range(8):
            self.pieces.append(Piece(i, 6, "W", "P"))
        self.pieces.append(Piece(0, 7, "W", "R"))
        self.pieces.append(Piece(7, 7, "W", "R"))
        self.pieces.append(Piece(1, 7, "W", "N"))
        self.pieces.append(Piece(6, 7, "W", "N"))
        self.pieces.append(Piece(2, 7, "W", "B"))
        self.pieces.append(Piece(5, 7, "W", "B"))
        self.pieces.append(Piece(3, 7, "W", "Q"))
        self.pieces.append(Piece(4, 7, "W", "K"))
    
    def piece_at(self, x, y):
        for piece in self.pieces:
            if piece.x == x and piece.y == y:
                return piece
        return None
    
    def piece_pos(self, p, c):
        for piece in self.pieces:
            if piece.piece == p and piece.color == c:
                return piece.x, piece.y
        return None
    
    
    def is_in_check(self,color):
        king_pos = self.piece_pos("K", color)
        for piece in self.pieces:
            if piece.color != color:
                if piece.is_valid_move(king_pos[0], king_pos[1],self):
                    return True
        return False

    def valid_moves(self,color):
        valid_moves = []
        for piece in self.pieces:
            if piece.color == color:
                for r in range(8):
                    for c in range(8):
                        if piece.is_valid_move(c, r,self):
                            valid_moves.append((piece,c,r))

        final_valid_moves = []
        for move in valid_moves:
            hypothetical_board = Board()
            hypothetical_board.pieces = deepcopy(self.pieces)
            move_piece = move[0]
            move_x = move[1]
            move_y = move[2]
            hypo_piece = hypothetical_board.piece_at(move_piece.x, move_piece.y)
            
            if move_piece.piece == "K" and abs(move_x - move_piece.x) == 2:
                if hypothetical_board.is_in_check(move_piece.color):
                    continue
                else:
                    hypo_piece.x= (move_x - hypo_piece.x)//2 + hypo_piece.x
                    if hypothetical_board.is_in_check(move_piece.color):
                        continue
                    hypo_piece.x = move_x
                    if hypothetical_board.is_in_check(move_piece.color):
                        continue     
            
            hypo_piece.move(move_x, move_y, hypothetical_board)

            if not hypothetical_board.is_in_check(color):
                final_valid_moves.append(move)

        return final_valid_moves
    

    
    def draw(self, screen, player_color, selected_piece=None, last_move=None):
        for row in range(8):
            for col in range(8):
                color = (255, 255, 255) # White
                if (row + col) % 2 != 0:
                    color = (50, 100, 50) # Faded deep green
                pygame.draw.rect(screen, color, (100+col*size, row*size, size, size))
        
        
        white_king_pos = self.piece_pos("K", "W")
        black_king_pos = self.piece_pos("K", "B")
        black_king_pos_x, black_king_pos_y = 0,0
        white_king_pos_x, white_king_pos_y = 0,0
        if player_color == "B":
            white_king_pos_x = 7 - white_king_pos[0]
            white_king_pos_y = 7 - white_king_pos[1]
            black_king_pos_x = 7 - black_king_pos[0]
            black_king_pos_y = 7 - black_king_pos[1]
        elif player_color == "W":
            white_king_pos_x = white_king_pos[0]
            white_king_pos_y = white_king_pos[1]
            black_king_pos_x = black_king_pos[0]
            black_king_pos_y = black_king_pos[1]
        if self.is_in_check("W"):
            pygame.draw.circle(screen, (255, 0, 0), (100 + white_king_pos_x * size + size / 2, white_king_pos_y * size + size / 2), size/2)
        if self.is_in_check("B"):
            pygame.draw.circle(screen, (255, 0, 0), (100 + black_king_pos_x * size + size / 2, black_king_pos_y * size + size / 2), size/2)
        
        # Draw valid move indicators if a piece is selected
        if last_move:
            last_x, last_y = last_move['to_x'], last_move['to_y']
            if player_color == "B":
                last_x = 7 - last_x
                last_y = 7 - last_y
            # Create a translucent yellow surface
            highlight = pygame.Surface((size, size), pygame.SRCALPHA)
            pygame.draw.rect(highlight, (255, 255, 0, 100), highlight.get_rect())
            screen.blit(highlight, (100 + last_x * size, last_y * size))
        
        for piece in self.pieces:
            piece.draw(screen, player_color)
        
        self.side_menu.draw(screen)

        if selected_piece:
            valid_moves = self.valid_moves(selected_piece.color)
            for move in valid_moves:
                if move[0] == selected_piece:
                    c,r = move[1], move[2]
                    if player_color == "B":
                        c = 7 - c
                        r = 7 - r
                    center_x = int(100 + c * size + size / 2)
                    center_y = int(r * size + size / 2)
                    radius = int(size / 6)
                    if self.piece_at(move[1], move[2]) == None:
                        pygame.draw.circle(screen, (150, 150, 150, 150), (center_x, center_y), radius) # Added alpha for transparency
                    else:
                        pygame.draw.circle(screen, (150, 150, 150, 150), (center_x, center_y), radius*3, width=5) # Draw hollow circle with 2px width

class Promotion:
    def __init__(self, x, y, color):
        self.color = color
        self.piece_types = ['Q', 'R', 'B', 'N'] # Order matters
        
        # Menu size: 4 squares wide, 1 square tall
        self.menu_width = size * len(self.piece_types)
        self.menu_height = size
        
        # Position below the pawn's final square
        # Adjust x by board offset (100)
        if self.color == "B":
            x = 7-x
            y = 7-y
        menu_pixel_x = 100 + x * size
        # Position below white pawn (y=7), or above black pawn (y=0)
        menu_pixel_y = (y + 1) * size if y ==0 else (y - 1) * size
        # Ensure menu stays within screen bounds vertically
        if menu_pixel_y + self.menu_height > 600:
             menu_pixel_y = 600 - self.menu_height
        if menu_pixel_y < 0:
             menu_pixel_y = 0 # Should ideally appear above black pawn, adjust if needed
        if menu_pixel_x + self.menu_width > 800:
            menu_pixel_x = 800 - self.menu_width
        if menu_pixel_x < 0:
            menu_pixel_x = 0

        
        self.menu_rect = pygame.Rect(menu_pixel_x, menu_pixel_y, self.menu_width, self.menu_height)
        
        self.images = {}
        for piece_type in self.piece_types:
            try:
                img_path = resource_path(f"assets/{self.color}{piece_type}.svg") 
                img = pygame.image.load(img_path) 
                # Scale based on OS
                if platform.system() == 'Darwin': # macOS
                    target_size = (int(size), int(size))
                else: # Windows or other OS
                    target_size = (200, 200)
                self.images[piece_type] = pygame.transform.smoothscale(img, target_size)
            except pygame.error as e:
                print(f"Error loading promotion image {img_path}: {e}")
                self.images[piece_type] = None

    def draw(self, screen):
        pygame.draw.rect(screen, (200, 200, 200), self.menu_rect) # Background
        pygame.draw.rect(screen, (0, 0, 0), self.menu_rect, 2) # Border

        # Draw promotion piece options horizontally
        for i, piece_type in enumerate(self.piece_types):
            if self.images[piece_type]:
                # Position images side-by-side within the menu_rect
                img_rect = pygame.Rect(self.menu_rect.left + i * size, self.menu_rect.top, size, size)
                screen.blit(self.images[piece_type], img_rect)

    def get_choice(self, mouse_x, mouse_y):
        if self.menu_rect.collidepoint(mouse_x, mouse_y):
            # Calculate which horizontal slot was clicked
            relative_x = mouse_x - self.menu_rect.left
            choice_index = int(relative_x // size)
            if 0 <= choice_index < len(self.piece_types):
                return self.piece_types[choice_index]
        return None
    
class Gameover:
    def __init__(self, message):
        self.message = message
    
    def draw(self, screen):
        main_font = pygame.font.Font(None, 100)
        small_font = pygame.font.Font(None, 30) # Smaller font for play again
        
        # Draw a filled rectangle behind the text
        rect_width = 600
        rect_height = 150
        rect_x = 400 - rect_width//2  # Center horizontally
        rect_y = 300 - rect_height//2 # Center vertically
        pygame.draw.rect(screen, (50, 50, 50), (rect_x, rect_y, rect_width, rect_height))
        pygame.draw.rect(screen, (255, 255, 255), (rect_x, rect_y, rect_width, rect_height), 2)  # White border
        
        # Render and potentially scale the main game over message
        text = main_font.render(self.message, True, (255, 255, 255))
        text_rect = text.get_rect(center=(400, 300)) # Initial position
        text_width = text.get_width()
        text_height = text.get_height()
        if text_width > rect_width - 40:
            scale_factor = (rect_width - 40) / text_width
            scaled_text = pygame.transform.smoothscale(text, 
                (int(text_width * scale_factor), int(text_height * scale_factor)))
            text_rect = scaled_text.get_rect(center=(400, 300)) # Recalculate rect after scaling
            screen.blit(scaled_text, text_rect)
        else:
            screen.blit(text, text_rect)
        
        # Render and position the "Play Again" text below the main message
        play_again_surf = small_font.render("Enter to Play Again", True, (200, 200, 200)) # Slightly dimmer color
        # Position below the main text_rect, centered horizontally
        play_again_rect = play_again_surf.get_rect(centerx=text_rect.centerx, top=text_rect.bottom + 10) 
        screen.blit(play_again_surf, play_again_rect)

class SideMenu:
    def __init__(self):
        self.font = pygame.font.Font(None, 30)
        self.resign_text = self.font.render("Resign", True, (255, 255, 255))
        self.draw_text = self.font.render("Draw", True, (255, 255, 255))
        self.resign_button = pygame.Rect(710, 100, 80, 40)
        self.draw_button = pygame.Rect(710, 160, 80, 40)
        self.resign_rect = self.resign_text.get_rect(center=self.resign_button.center)
        self.draw_rect = self.draw_text.get_rect(center=self.draw_button.center)

    def draw(self, screen):
        # Draw button backgrounds
        pygame.draw.rect(screen, (50, 50, 50), self.resign_button)
        pygame.draw.rect(screen, (50, 50, 50), self.draw_button)

        # Draw button borders
        pygame.draw.rect(screen, (255, 255, 255), self.resign_button, 2)
        pygame.draw.rect(screen, (255, 255, 255), self.draw_button, 2)

        # Draw text
        screen.blit(self.resign_text, self.resign_rect)
        screen.blit(self.draw_text, self.draw_rect)
    
    def highlight_draw(self, screen):
        pygame.draw.rect(screen, (255, 255, 0), self.draw_button, 0)
        newTest = self.font.render("Draw", True, (0,0,0))
        screen.blit(newTest, self.draw_rect)

        

def do_move(move_data, board, my_turn, turn, fifty_move_rule, drawed, board_states, last_move):
        print(f"my_turn: {my_turn}")
        print(f"turn: {turn}")
        piece_to_move = board.piece_at(move_data['from_x'], move_data['from_y'])
        last_move = move_data
        oldlen = len(board.captured_pieces)
        piece_to_move.move(move_data['to_x'], move_data['to_y'],board)
        my_turn = not my_turn if my_turn != None else None
        move_sound.play()
        is_pawn_move = piece_to_move.piece == "P"
        is_capture = len(board.captured_pieces) > oldlen
        if 'promotion' in move_data and move_data['promotion']:
            piece_to_move.piece = move_data['promotion']
        if is_capture and capture_sound:
            capture_sound.play()
        if is_pawn_move or is_capture:
            fifty_move_rule = 0
        else:
            fifty_move_rule += 1
        turn = "B" if turn == "W" else "W"
        drawed = set([])
        board_states.append(deepcopy(board.pieces))
        return my_turn, turn, fifty_move_rule, drawed, board_states, last_move

def online_poller(session, site, server_ip, stop_event, move_q, offer_q, players_q, poll_interval_sec, initial_last_move, initial_last_offer, initial_last_players):

    current_last_move = initial_last_move
    current_last_offer = initial_last_offer
    current_last_players = initial_last_players
    while not stop_event.is_set():
        next_poll_time = time.monotonic() + poll_interval_sec
        try:
            # --- Poll for players ---
            try:
                players_response = session.get(f"{site}{server_ip}/players", timeout=5) # Shorter timeout for non-critical polls
                players_response.raise_for_status() # Check for HTTP errors
                players_data = players_response.json()
                # Check if players_data is valid before queuing (e.g., is it a list?)
                if players_data != current_last_players:
                    if isinstance(players_data, list):
                        players_q.put(players_data) # Put player list in queue
                        current_last_players = players_data
                        print(f"[PollerThread] Received player data: {players_data}")
                    else:
                     print(f"[PollerThread] Received non-list player data: {players_data}")
            except requests.exceptions.RequestException as e:
                print(f"[PollerThread] Network error polling players: {e}")
                players_q.put("ERROR_DISCONNECTED") # Signal potential disconnect
            except json.JSONDecodeError as e:
                print(f"[PollerThread] JSON decode error polling players: {e}")
            except Exception as e:
                 print(f"[PollerThread] Unexpected error polling players: {e}")


            # --- Poll for latest move ---
            try:
                move_response = session.get(f"{site}{server_ip}/latest-move", timeout=10) # Longer timeout for critical move data
                move_response.raise_for_status()
                move_data = move_response.json() # Can be None/null
                # Only put move in queue if it's new (different from last known)
                # Handles case where server sends null initially or after reset
                if move_data != current_last_move:
                    print(f"[PollerThread] New move detected: {move_data}")
                    current_last_move = move_data # Update local state for comparison
                    move_q.put(move_data)
            except requests.exceptions.RequestException as e:
                print(f"[PollerThread] Network error polling move: {e}")
                # Consider if move errors should also signal disconnect
            except json.JSONDecodeError as e:
                print(f"[PollerThread] JSON decode error polling move: {e}")
            except Exception as e:
                 print(f"[PollerThread] Unexpected error polling move: {e}")

            # --- Poll for latest offer ---
            try:
                offer_response = session.get(f"{site}{server_ip}/latest-offer", timeout=10)
                offer_response.raise_for_status()
                offer_data = offer_response.json() # Can be None/null
                # Only put offer in queue if it's new (different from last known)
                if offer_data != current_last_offer:
                    print(f"[PollerThread] New offer detected: {offer_data}")
                    current_last_offer = offer_data # Update local state for comparison
                    offer_q.put(offer_data)
            except requests.exceptions.RequestException as e:
                print(f"[PollerThread] Network error polling offer: {e}")
            except json.JSONDecodeError as e:
                 print(f"[PollerThread] JSON decode error polling offer: {e}")
            except Exception as e:
                 print(f"[PollerThread] Unexpected error polling offer: {e}")


        except Exception as e:
            # Catch-all for safety within the outer try
            print(f"[PollerThread] Unexpected error during polling cycle: {e}")

        # Wait until the next poll time, checking stop_event frequently
        while time.monotonic() < next_poll_time and not stop_event.is_set():
            time.sleep(0.05) # Sleep briefly

def online_sender(session, stop_event, send_q, site, server_ip):
    while not stop_event.is_set():
        try:
            send_data = send_q.get(timeout=0.5)
            if send_data:
                response = session.post(f"{site}{server_ip}/move", json=send_data)
                response.raise_for_status()
        except Empty:
            pass



def main():
    global opponent_ip, is_host, move_queue, pending_promotion_move, move_sound, capture_sound, polling_thread, sender_thread, poller_stop_event, opponent_move_queue, offer_queue, player_status_queue, send_queue, sender_stop_event # Access global state

    pygame.init()
    pygame.font.init() # Ensure font module is initialized
    pygame.mixer.init() # Initialize sound mixer
    session = Session()
    try:
        move_sound = pygame.mixer.Sound(resource_path("assets/move.mp3")) # Load move sound
    except pygame.error as e:
        print(f"Could not load sound file: {e}")
    try:
        capture_sound = pygame.mixer.Sound(resource_path("assets/capture.mp3")) # Load capture sound
    except pygame.error as e:
        print(f"Could not load sound file: {e}")
    screen = pygame.display.set_mode((800, 600))
    pygame.display.set_caption("Chesspy")
    
    
    # --- Font for Setup ---
    setup_font = pygame.font.Font(None, 48)
    input_font = pygame.font.Font(None, 36)
    input_box = pygame.Rect(200, 200, 400, 40) # Position for IP input
    input_text = ''
    input_active = False
    display_ip_message = "" # To show host's IP
    site = "https://chess-server-5mll.onrender.com/"
    game_state = "setup" # New initial state

    last_poll_time = 0
    last_move_poll_time = 0
    last_connection_poll_time = 0
    poll_interval_ms = 100 # Check ~10 times per second (1000ms / 10Hz)
    while True:
        pygame.time.Clock().tick(30) # Limit FPS during setup
        if game_state == "setup":
            setup_message = "Choose: Online (O) or Local (L)"
            ip_prompt = "Enter Host IP:"
            bottom_message = ""
            board = Board()
            selected_piece = None
            turn = "W"
            board_states = [deepcopy(board.pieces)]
            fifty_move_rule = 0
            promotion_handler = None
            gameover_handler = None
            http_server = None 
            player_color = None
            last_move = None
            drawed = set([])
            resigned = None
            my_turn = None
            server_ip = ""
            players = []
            online = None
            play_again_active = False
            local_enter_pressed = False

            screen.fill((30, 30, 30))
            text_surface = setup_font.render(setup_message, True, (255, 255, 255))
            text_rect = text_surface.get_rect(center=(400, 150))
            screen.blit(text_surface, text_rect)
            pygame.display.update() 

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    return
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_o:
                        game_state = "online_setup"
                        online = True
                        setup_message = "Create Room (C) or Join Room (J)"
                    elif event.key == pygame.K_l:
                        game_state = "local_setup"
                        setup_message = "Choose: Host (H) or Join (J)"
                        online = False
            
                
        elif game_state == "online_setup":
            ip_prompt = "Enter Room Code:"
            screen.fill((30, 30, 30)) # Dark background
            text_surface = setup_font.render(setup_message, True, (255, 255, 255))
            text_rect = text_surface.get_rect(center=(400, 150))
            bottom_message_surface = setup_font.render(bottom_message, True, (255, 255, 255))
            bottom_message_rect = bottom_message_surface.get_rect(center=(400, 300))
            screen.blit(bottom_message_surface, bottom_message_rect)

            if input_active: # Still need to enter IP
                setup_message = ""
                prompt_surf = input_font.render(ip_prompt, True, (255, 255, 255))
                prompt_rect = prompt_surf.get_rect(midbottom=(400, input_box.top - 10))
                screen.blit(prompt_surf, prompt_rect)

                pygame.draw.rect(screen, (200, 200, 200) if input_active else (100, 100, 100), input_box, 2)
                input_surface = input_font.render(input_text, True, (255, 255, 255))
                screen.blit(input_surface, (input_box.x + 5, input_box.y + 5))
                input_box.w = max(400, input_surface.get_width() + 10)
            else: # Draw the B/W choice message only if not asking for IP
                screen.blit(text_surface, text_rect)

            if setup_message == "Waiting for opponent...":
                current_time = pygame.time.get_ticks()
                if current_time - last_poll_time >= 50:
                    last_poll_time = current_time
                    try:
                        response = session.get(f"{site}{server_ip}/players")
                        server_players = response.json()
                        if len(server_players) == 2:
                            # --- Start Polling Thread ---
                            print("test1")
                            if polling_thread is None or not polling_thread.is_alive():
                                poller_stop_event.clear()
                                # Pass initial last_move state (should be None at game start)
                                # Pass initial offer state (None)
                                
                                polling_thread = threading.Thread(target=online_poller,
                                                                args=(session, site, server_ip, poller_stop_event,
                                                                        opponent_move_queue, offer_queue, player_status_queue,
                                                                        poll_interval_ms / 1000.0, # Convert ms to seconds
                                                                        None, None, None), # Pass None initially
                                                                daemon=True)
                                polling_thread.start()
                            if sender_thread is None or not sender_thread.is_alive():
                                sender_stop_event.clear()
                                sender_thread = threading.Thread(target=online_sender,
                                                                args=(session, sender_stop_event, send_queue, site, server_ip),
                                                                daemon=True)
                                sender_thread.start()
                            print("test2")
                            print("Game state set to normal")
                            game_state = "normal"
                    except:
                        pass

            pygame.display.update()

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if server_ip != "":
                        response = session.delete(f"{site}{server_ip}/players")
                        response = session.get(f"{site}{server_ip}/delete")
                        poller_stop_event.set()
                        sender_stop_event.set()
                    pygame.quit()
                    return
                elif event.type == pygame.KEYDOWN and not input_active:
                    if event.key == pygame.K_j:
                        player_color = "B"
                        my_turn = False
                        input_active = True
                    elif event.key == pygame.K_c:
                        player_color = "W"
                        my_turn = True
                        server_ip = random.randint(1000, 9999)
                        response = session.get(f"{site}{server_ip}/create")
                        while response.json()["message"] != "Room created":
                            server_ip = random.randint(1000, 9999)
                            response = session.get(f"{site}{server_ip}/create")
                        setup_message = "Waiting for opponent..."
                        bottom_message = f"Room Code: {server_ip}"
                        players.append(player_color)
                        response = session.post(f"{site}{server_ip}/players", json=players)

                elif event.type == pygame.KEYDOWN and input_active:
                    if event.key == pygame.K_RETURN:
                        server_ip = input_text
                        response = session.get(f"{site}{server_ip}/players")
                        try:
                            server_players = response.json()
                            if len(server_players) >= 2 or server_players.count(player_color) >= 1:
                                bottom_message = "Room is full"
                                server_ip = ""
                                input_text = ""
                                continue
                            for player in server_players:
                                players.append(player)
                            players.append(player_color)
                            ### send players to server
                            response = session.post(f"{site}{server_ip}/players", json=players)
                            input_active = False
                            setup_message = "Waiting for opponent..."
                            bottom_message = ""
                        except:
                            bottom_message = "Room not found"
                            server_ip = ""
                            input_text = ""
                            continue
                    elif event.key == pygame.K_BACKSPACE:
                        input_text = input_text[:-1]
                    else:
                        input_text += event.unicode


        elif game_state == "local_setup":
            screen.fill((30, 30, 30)) # Dark background
            text_surface = setup_font.render(setup_message, True, (255, 255, 255))
            text_rect = text_surface.get_rect(center=(400, 150))
            screen.blit(text_surface, text_rect)
        

            # --- Host Specific Logic during Setup ---
            if is_host:
                if not client_connected_event.is_set():
                    # Display Host's IP while waiting
                    if display_ip_message:
                        ip_surf = input_font.render(display_ip_message, True, (200, 200, 200))
                        ip_rect = ip_surf.get_rect(center=(400, 250))
                        screen.blit(ip_surf, ip_rect)

                    # Keep displaying the waiting message
                    text_surface = setup_font.render(setup_message, True, (255, 255, 255))
                    text_rect = text_surface.get_rect(center=(400, 150))
                    screen.blit(text_surface, text_rect)
                else:
                    # Client has connected, proceed to game
                    print("Client connection confirmed by event.")
                    game_state = "normal"
                    # Optional: Update message to reflect game start
                    setup_message = "Opponent connected! White's turn."

            # --- Join Specific Logic during Setup ---
            elif is_host is False:
                if not opponent_ip: # Still need to enter IP
                    prompt_surf = input_font.render(ip_prompt, True, (255, 255, 255))
                    prompt_rect = prompt_surf.get_rect(midbottom=(400, input_box.top - 10))
                    screen.blit(prompt_surf, prompt_rect)

                    pygame.draw.rect(screen, (200, 200, 200) if input_active else (100, 100, 100), input_box, 2)
                    input_surface = input_font.render(input_text, True, (255, 255, 255))
                    screen.blit(input_surface, (input_box.x + 5, input_box.y + 5))
                    input_box.w = max(400, input_surface.get_width() + 10) # Resize box
                else: # IP entered, waiting for first move (already set game_state=normal)
                    game_state = "normal"
                    text_surface = setup_font.render(setup_message, True, (255, 255, 255))
                    text_rect = text_surface.get_rect(center=(400, 150))
                    screen.blit(text_surface, text_rect)

            # --- Initial Choice (Host/Join) Logic ---
            elif is_host is None:
                # Display setup message
                text_surface = setup_font.render(setup_message, True, (255, 255, 255))
                text_rect = text_surface.get_rect(center=(400, 150))
                screen.blit(text_surface, text_rect)


            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if http_server:
                        http_server.shutdown() # Attempt graceful shutdown
                    pygame.quit()
                    
                    return

                if event.type == pygame.KEYDOWN:
                    if is_host is None: # Only process H/J if choice hasn't been made
                        if event.key == pygame.K_d:
                            is_host = None
                            my_turn = None
                            player_color = "W"
                            game_state = "normal"
                        elif event.key == pygame.K_h:
                            is_host = True
                            my_turn = True # Host is White, starts
                            player_color = "W"
                            http_server = start_server()
                            if http_server:
                                try: # Get local IP to display
                                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                    s.connect(("8.8.8.8", 80)) # Connect to external server to find outgoing IP
                                    local_ip = s.getsockname()[0]
                                    s.close()
                                    display_ip_message = f"Hosting! Your IP: {local_ip}. Waiting for opponent..."
                                    setup_message = "Waiting for opponent to connect..." # Updated message
                                except Exception as e:
                                    print(f"Could not determine local IP: {e}")
                                    display_ip_message = "Hosting! Could not determine IP. Opponent must know it."
                                    setup_message = "Waiting for opponent to connect..." # Updated message
                                    # game_state = "normal" # Host does NOT start game immediately anymore
                            else:
                                # Server failed to start, reset state
                                is_host = None
                                my_turn = None
                                player_color = None
                                setup_message = "Server start failed. Try again? (H/J)"

                        elif event.key == pygame.K_j:
                            is_host = False
                            my_turn = False # Client is Black, waits
                            player_color = "B"
                            # --- Client also needs to start a server ---
                            print("Attempting to start server as client...")
                            http_server = start_server()
                            if not http_server:
                                # Server failed to start for client, cannot proceed
                                is_host = None # Reset state
                                my_turn = None
                                player_color = None
                                setup_message = "Server start failed. Try again? (H/J)"
                                print("Error: Client failed to start its HTTP server.")
                                continue # Go back to loop start to display error
                            # ---------------------------------------------
                            input_active = True
                            setup_message = "" # Clear host/join prompt
                        


                    elif is_host is False and input_active: # If joining and input is active
                        if event.key == pygame.K_RETURN:
                            temp_ip = input_text # Store temporarily
                            print(f"Attempting connection ping to host: {temp_ip}")
                            # Send connection ping and check result
                            if send_connection_ping(temp_ip, 8000):
                                # Success!
                                opponent_ip = temp_ip # Set the global opponent_ip
                                input_active = False
                                game_state = "normal" # Start the game loop, waiting for first move
                                setup_message = "Connected! Waiting for White's move..." # Update status
                                print(f"Successfully connected to host: {opponent_ip}")
                            else:
                                # Failed to connect
                                print(f"Error: Could not connect to host at {temp_ip}. Please check the IP and try again.")
                                setup_message = f"Failed to connect to {temp_ip}. Re-enter Host IP:" # Display error on screen
                                opponent_ip = None # Ensure opponent_ip is not set
                                input_text = "" # Optionally clear the input box for re-entry
                                # Keep input_active = True
                                # Keep game_state = "setup"
                        elif event.key == pygame.K_BACKSPACE:
                            input_text = input_text[:-1]
                        else:
                            input_text += event.unicode

                if event.type == pygame.MOUSEBUTTONDOWN and is_host is False:
                    if input_box.collidepoint(event.pos):
                        input_active = True
                    else:
                        input_active = False
            pygame.display.update()
                




        elif game_state == "normal" or game_state == "gameover" or game_state == "promotion_pending":
            # --- Check for incoming moves ---
            current_time = pygame.time.get_ticks()


            if not my_turn and game_state == "normal": # Only process if it's opponent's turn and game is running
                
                if not online:
                    try:
                        move_data = move_queue.get_nowait() # Check queue without blocking
                        print(f"Processing move from queue: {move_data}")
                        my_turn, turn, fifty_move_rule, drawed, board_states, last_move = do_move(move_data, board, my_turn, turn, fifty_move_rule, drawed, board_states, last_move)

                    except Empty:
                        pass # No move received yet
                    except Exception as e:
                        print(f"Error processing move from queue: {e}")
                        # Potentially set an error state in the game
                else:
                    try:
                        response = session.get(f"{site}{server_ip}/latest-move")
                        move_data = response.json()
                        if move_data and move_data != last_move:
                            if current_time - last_move_poll_time >= poll_interval_ms:
                                    last_move_poll_time = current_time
                                    print(f"Processing move from server: {move_data}")
                                    my_turn, turn, fifty_move_rule, drawed, board_states, last_move = do_move(move_data, board, my_turn, turn, fifty_move_rule, drawed, board_states, last_move)
                    except Exception as e:
                        print(f"Error processing move from server: {e}")
                        # Potentially set an error state in the game

            # --- Drawing ---
            screen.fill((0, 0, 0))
            board.draw(screen,player_color, selected_piece, last_move)
            if game_state == "promotion_pending":
                if promotion_handler: # Check if handler exists
                    promotion_handler.draw(screen)
            if game_state == "gameover":
                if gameover_handler: # Check if handler exists
                    gameover_handler.draw(screen)
            if len(drawed) != 0:
                board.side_menu.highlight_draw(screen)
            pygame.display.update() # Use update for potentially smaller screen area changes

            if board.valid_moves(turn) == []:
                game_state = "gameover"
                if board.is_in_check(turn):
                    winner = "Black" if turn == "W" else "White"
                    gameover_handler = Gameover(f"{winner} wins by checkmate")
                else:
                    gameover_handler = Gameover(f"Stalemate")

            if len(board.pieces) == 2:
                game_state = "gameover"
                gameover_handler = Gameover("Draw by insufficient material")
            elif len(board.pieces) == 3 and (board.piece_pos("B","W") != None or board.piece_pos("B","B") != None or board.piece_pos("N","W") != None or board.piece_pos("N","B") != None):
                game_state = "gameover"
                gameover_handler = Gameover("Draw by insufficient material")
            elif len(board.pieces) == 4 and ((board.piece_pos("B","W") != None and board.piece_pos("B","B") != None) or (board.piece_pos("N","W") != None and board.piece_pos("N","B") != None)):
                game_state = "gameover"
                gameover_handler = Gameover("Draw by insufficient material")
            elif len(board.pieces) == 4 and ((board.piece_pos("B","W") != None and board.piece_pos("N","B") != None) or (board.piece_pos("N","W") != None and board.piece_pos("B","B") != None)):
                game_state = "gameover"
                gameover_handler = Gameover("Draw by insufficient material")

            for state in board_states:
                if board_states.count(state) == 3:
                    game_state = "gameover"
                    gameover_handler = Gameover("Draw by repetition")

            if fifty_move_rule == 100:
                game_state = "gameover"
                gameover_handler = Gameover("Draw by fifty move rule")

            if online:
                if game_state != "gameover":
                    try:
                        offer = offer_queue.get_nowait()
                    except Empty:
                        offer = None
                    if offer != None and offer['color'] != player_color:
                        if offer['type'] == "resign":
                            resigned = offer['color']
                        elif offer['type'] == "draw":
                            drawed.add(offer['color'])
                        response = session.delete(f"{site}{server_ip}/offer")
                try:
                    server_players = player_status_queue.get_nowait()
                except Empty:
                    server_players = None
                except Exception as e:
                    server_players = []
                if server_players != None:
                    players = server_players
                if len(players)  < 2:
                    print("Opponent disconnected.") # Add console log
                    poller_stop_event.set()
                    sender_stop_event.set()
                    game_state = "setup"
            
            if resigned != None or resign.is_set():
                game_state = "gameover"
                if resigned == None:
                    resigned = "W" if player_color == "B" else "B"
                    resign.clear()
                winner = "Black" if resigned == "W" else "White"
                gameover_handler = Gameover(f"{winner} wins by resignation")
            
            if draw.is_set():
                drawed.add("W" if player_color == "B" else "B")
                draw.clear()

            
            if len(drawed) == 2:
                game_state = "gameover"
                gameover_handler = Gameover("Draw by agreement")
            

            if game_state == "gameover":
                drawed = set([])
                resigned = None
                last_move = None
                selected_piece = None
                
                current_time = pygame.time.get_ticks()
                if current_time - last_poll_time >= poll_interval_ms and online:
                    last_poll_time = current_time
                    try:
                        response = session.get(f"{site}{server_ip}/latest-offer")
                        offer = response.json()
                        if offer != None and offer['type'] == "play_again" and offer['color'] != player_color:
                            play_again_active = True
                            response = session.delete(f"{site}{server_ip}/offer")
                    except Exception as e:
                        continue
            
                if play_again_active or local_enter_pressed:
                    if not online and not play_again.is_set():
                        send_offer(opponent_ip, 8000, "play_again", player_color)
                    elif online and local_enter_pressed:
                        response = session.post(f"{site}{server_ip}/offer", json={"type": "play_again", "color": player_color})
                    game_state = "normal"
                    board = Board()
                    board_states = [deepcopy(board.pieces)]
                    fifty_move_rule = 0
                    promotion_handler = None
                    gameover_handler = None
                    turn = "W"
                    if my_turn == None:
                        my_turn = None
                    elif player_color == "B":
                        my_turn = False
                    else:
                        my_turn = True
                    play_again.clear()
                    local_enter_pressed = False
                    play_again_active = False
                    if online:
                        response = session.delete(f"{site}{server_ip}/move")

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if http_server:
                        http_server.shutdown()
                    pygame.quit()
                    if online:
                        response = session.delete(f"{site}{server_ip}/players")
                        response = session.delete(f"{site}{server_ip}/move")
                        response = session.delete(f"{site}{server_ip}/offer")
                        response = session.get(f"{site}{server_ip}/delete")
                        poller_stop_event.set()
                        sender_stop_event.set()
                    return

                if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN and game_state == "gameover":
                    local_enter_pressed = True
                    
                if event.type == pygame.MOUSEBUTTONDOWN and game_state != "gameover":
                    mouse_x, mouse_y = pygame.mouse.get_pos()
                    if board.side_menu.resign_button.collidepoint(mouse_x, mouse_y):
                        resigned = player_color
                        if my_turn != None and not online:
                            send_offer(opponent_ip, 8000, "resign", player_color)
                        elif online:
                            print(f"Sending resign offer to server")
                            response = session.post(f"{site}{server_ip}/offer", json={"type": "resign", "color": player_color})
                        continue
                    elif board.side_menu.draw_button.collidepoint(mouse_x, mouse_y):
                        drawed.add(player_color)
                        if my_turn != None and not online:
                            send_offer(opponent_ip, 8000, "draw", player_color)
                        elif online:
                            response = session.post(f"{site}{server_ip}/offer", json={"type": "draw", "color": player_color})
                        continue


                if event.type == pygame.MOUSEBUTTONDOWN and (my_turn != False) and game_state != "gameover":
                    mouse_x, mouse_y = pygame.mouse.get_pos()
                    print(f"{game_state}")

                    # --- Promotion Handling ---
                    if game_state == "promotion_pending":
                        if promotion_handler and promotion_handler.menu_rect.collidepoint(mouse_x, mouse_y):
                            choice = promotion_handler.get_choice(mouse_x, mouse_y)
                            selected_piece = board.piece_at(pending_promotion_move['to_x'], pending_promotion_move['to_y'])
                            print(f"choice: {choice} {selected_piece} {pending_promotion_move}")
                            if choice and selected_piece and pending_promotion_move:
                                print(f"Promotion choice: {choice}")
                                promotion_handler = None # Clear handler
                                move_data = pending_promotion_move                            
                                pending_promotion_move = None
                                move_data['promotion'] = choice
                                if not online:
                                    send_move(opponent_ip, 8000, move_data)
                                else:
                                    send_queue.put(move_data)
                                selected_piece.piece = choice
                                selected_piece = None
                                if my_turn != None:
                                    my_turn = not my_turn

                                
                                game_state = "normal" # Return to normal gameplay

                                print(f"Sent promoted move. It's now {turn}'s turn (my_turn={my_turn}).")
                            # If click was in menu but didn't yield a choice, do nothing yet
                        # Click outside promotion menu while pending? -> Maybe cancel selection? (For now, ignore)
                        continue # Don't process board clicks while promotion menu is up


                    # --- Normal Board Click Handling ---
                    if game_state == "normal":
                        board_width_pixels = 8 * size
                        board_height_pixels = 8 * size
                        if 100 <= mouse_x < 100 + board_width_pixels and 0 <= mouse_y < board_height_pixels:
                            # Convert mouse position to board coordinates
                            col = int((mouse_x - 100) // size)
                            row = int(mouse_y // size)

                        # Now handle selection/movement *only if* click was on the board
                        if player_color == "B":
                            col = 7 - col
                            row = 7 - row
                        if selected_piece != None:
                            if board.valid_moves(selected_piece.color).count((selected_piece,col,row)) > 0:
                                move_data = {
                                        'from_x': selected_piece.x,
                                        'from_y': selected_piece.y,
                                        'to_x': col,
                                        'to_y': row,
                                        'promotion': None, # Default to no promotion
                                        'fifty_move': fifty_move_rule # Include current rule state
                                    }

                                print(f"selected_piece.piece: {selected_piece.piece} {row}")
                                my_turn, turn, fifty_move_rule, drawed, board_states, last_move = do_move(move_data, board, my_turn, turn, fifty_move_rule, drawed, board_states, last_move)
                                if selected_piece.piece == "P" and (row == 0 or row == 7):
                                    print("Promotion condition met.")
                                    promotion_handler = Promotion(col, row, player_color)
                                    pending_promotion_move = move_data # Store move data, wait for choice
                                    if my_turn != None:
                                        my_turn = not my_turn
                                    game_state = "promotion_pending"

                                else:
                                    if not online:
                                        send_move(opponent_ip, 8000, move_data)
                                    else:
                                        send_queue.put(move_data)

                                                            
                            selected_piece = None # Clear the selected piece variable
                                    
                        for piece in board.pieces:
                            if piece.x == col and piece.y == row and piece.color == turn:
                                selected_piece = piece
                    else:
                        # Click was outside the board, deselect any selected piece
                        selected_piece = None
                    
                    print(selected_piece)

if __name__ == "__main__":
    main()

