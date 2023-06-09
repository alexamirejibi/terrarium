import argparse
import bisect
from math import sqrt
import math
import pygame
import random
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque, namedtuple
import numpy as np
import copy
import functools
import matplotlib.pyplot as plt
import pickle
import os

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


# Parameters
SCREEN_SIZE = 500
NUM_AGENTS = 25
AGENT_RADIUS = 5
SPEED = 8
PERCEPTION_RADIUS = SCREEN_SIZE
NUM_FOOD_LOCATIONS = 1
NUM_FOODS = 50

# device = torch.device('mps')
device = torch.device('cpu')
# device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print('Using device:', device)


# Initialize Pygame

class FoodLocation:
    def __init__(self, x, y, radius_per_food=1, num_foods=50):
        self.x = x
        self.y = y
        self.num_foods = num_foods
        self.num_foods_currently = self.num_foods
        self.radius_per_food = radius_per_food
        self.radius = self.calc_radius()

    def draw(self, screen):
        pygame.draw.circle(screen, (255, 0, 0), (self.x, self.y), self.radius)

    def reset_random(self):
        self.x = random.randint(0, SCREEN_SIZE)
        self.y = random.randint(0, SCREEN_SIZE)
        self.num_foods_currently = self.num_foods
        
    def calc_radius(self):
        return self.num_foods_currently * self.radius_per_food
    
    
    def collect(self):
        # self.num_foods_currently -= 1
        self.radius = self.calc_radius()
        
class Colony:
    def __init__(self, x, y, radius=40):
        self.x = x
        self.y = y
        self.radius = radius

    def draw(self, screen):
        pygame.draw.circle(screen, (255, 255, 255), (self.x, self.y), self.radius)
    
    def randomize(self):
        self.x = random.randint(0, SCREEN_SIZE)
        self.y = random.randint(0, SCREEN_SIZE)


class QNetwork(nn.Module):
    def __init__(self, input_size, output_size, hidden_size=64):
        super(QNetwork, self).__init__()
        self.layers = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, output_size)
        )

    def forward(self, x):
        return self.layers(x)

class Brain:
    def __init__(self, input_size, output_size, hidden_size=32, batch_size=32, memory_size=10000, gamma=0.99, lr=1e-3, device=device):
        self.device = device
        self.q_network = QNetwork(input_size, output_size, hidden_size).to(device)
        self.target_q_network = QNetwork(input_size, output_size, hidden_size).to(device)
        self.target_q_network.load_state_dict(self.q_network.state_dict())
        self.target_q_network.eval()

        self.optimizer = optim.Adam(self.q_network.parameters(), lr=lr)
        self.loss_fn = nn.MSELoss()

        self.memory = deque(maxlen=memory_size)
        self.batch_size = batch_size
        self.gamma = gamma
        self.experience = namedtuple("Experience", field_names=["state", "action", "reward", "next_state", "done"])


    def act(self, state, epsilon=0.1):
        if random.random() < epsilon:
            return random.randint(0, self.q_network.layers[-1].out_features - 1)
        else:
            state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.device)
            with torch.no_grad():
                q_values = self.q_network(state_tensor)
            return torch.argmax(q_values).item()


    def memorize(self, state, action, reward, next_state, done):
        experience = self.experience(state, action, reward, next_state, done)
        self.memory.append(experience)


    def update(self):
        if len(self.memory) < self.batch_size:
            return

        experiences = random.sample(self.memory, k=self.batch_size)
        states, actions, rewards, next_states, dones = zip(*experiences)

        states_tensor = torch.FloatTensor(states).to(self.device)
        actions_tensor = torch.LongTensor(actions).unsqueeze(1).to(self.device)
        rewards_tensor = torch.FloatTensor(rewards).unsqueeze(1).to(self.device)
        next_states_tensor = torch.FloatTensor(next_states).to(self.device)
        dones_tensor = torch.BoolTensor(dones).unsqueeze(1).to(self.device)

        q_values = self.q_network(states_tensor).gather(1, actions_tensor)
        next_q_values = self.target_q_network(next_states_tensor).max(1, keepdim=True)[0].detach()
        target_q_values = rewards_tensor + (self.gamma * next_q_values * (~dones_tensor))

        loss = self.loss_fn(q_values, target_q_values)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

    def update_target_q_network(self):
        self.target_q_network.load_state_dict(self.q_network.state_dict())

    def save_model(self, path):
        torch.save(self.q_network.state_dict(), path)

    def load_model(self, path):
        self.q_network.load_state_dict(torch.load(path))


# Agent class
class Agent:
    def __init__(self, x, y, radius, brain, colony):
        self.x = x
        self.y = y
        self.radius = radius
        self.color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        self.brain = brain
        self.colony = colony
        self.state = None
        self.hit_wall = 0
        self.max_objects = 1
        self.input_len = 7 + (2 * self.max_objects)
        self.output_len = 3
        self.total_rewards = 0
        self.has_food = 0
        self.pass_radius = self.radius * 20
        self.agent_passed_to = None
        self.steps_since_passing = 1
        self.agents_involved = set()


    def step(self, food_locations, total_reward, agents, epsilon=0.1):
        sensed_food = self.sense(food_locations)
        dx, dy = (0, 0)
        nearby_agent = self.sense_closest_agents(agents)
        if nearby_agent != None:
            dx, dy = (nearby_agent.x - self.x, nearby_agent.y - self.y)
            dx /= SCREEN_SIZE
            dy /= SCREEN_SIZE 
        input_data = [0.0] * self.input_len
        input_data[0] = self.x / SCREEN_SIZE
        input_data[1] = self.y / SCREEN_SIZE
        input_data[2] = (self.colony.x - self.x) / SCREEN_SIZE
        input_data[3] = (self.colony.y - self.y) / SCREEN_SIZE
        input_data[4] = dx 
        input_data[5] = dy
        input_data[6] = self.has_food
        for i, (dx, dy) in enumerate(sensed_food): # actually only 1 food
            input_data[7 + 2 * i] = dx / SCREEN_SIZE
            input_data[7 + 2 * i + 1] = dy / SCREEN_SIZE
        next_state = input_data

        if self.state is not None:
            reward = total_reward # reward from another agent dropping off food that was in this agent's hands at some point
            if not self.has_food:
                self.collect_food(food_locations)
            self.total_rewards += reward
            self.brain.memorize(self.state, self.action, reward, next_state, False)
        self.state = next_state
        self.action = self.brain.act(self.state, epsilon)
        dx, dy, pass_food_to_agent = self.action_to_movement(self.action, food_locations)
        x = self.x + SPEED * dx
        y = self.y + SPEED * dy
        
        self.steps_since_passing += 1
        if nearby_agent != None and self.has_food and pass_food_to_agent and not nearby_agent.has_food:
            # print("Passed food")
            # self.steps_since_passing = 1
            self.pass_food(nearby_agent)
        
        # ------------------------------

        # Keep the agent inside the screen boundaries
        self.hit_wall = 0
        if 0 <= x <= SCREEN_SIZE:
            self.x = x
        else:
            self.hit_wall = 1

        if 0 <= y <= SCREEN_SIZE:
            self.y = y
        else:
            self.hit_wall = 1
            
        # return signal
        
    def pass_food(self, agent):
        # print("passing food")
        if (not self.has_food) or agent.has_food:
            # print("cant pass")
            return
        # print("passing food")
        distance = np.sqrt((self.x - agent.x) ** 2 + (self.y - agent.y) ** 2)
        if distance < self.pass_radius:
            self.agent_passed_to = agent
            # print(self.agent_passed_to)
            self.steps_since_passing = 1
            self.has_food = 0
            agent.has_food = 1
            self.agents_involved.add(self)
            self.agents_involved.add(agent)
            agent.agents_involved.update(self.agents_involved)

    def action_to_movement(self, action, food_locations):
        dx, dy = 0, 0
        pass_food_to_agent = False
        x = 1e-6
        if action == 0:  # go towards food
            min_distance = 99999999
            closest_loc = None
            for f in food_locations:
                dx_f, dy_f = self.get_direction(f)
                distance_sq = dy_f ** 2 + dx_f ** 2
                if distance_sq < min_distance:
                    min_distance = distance_sq
                    dx = dx_f
                    dy = dy_f
            dx, dy = dx / ((abs(dx) + abs(dy)) + x), dy / ((abs(dx) + abs(dy)) + x)

        elif action == 1:  # go towards colony
            dx, dy = self.get_direction(self.colony)
            dx, dy = dx / ((abs(dx) + abs(dy)) + x), dy / ((abs(dx) + abs(dy)) + x)

        elif action == 2: # pass food
            pass_food_to_agent = True
            
        # elif action == 3:  # go to the middle point between colony and the closest food
        #     min_distance = 99999999
        #     closest_food = None
        #     for f in food_locations:
        #         dx_f, dy_f = self.get_direction(f)
        #         distance_sq = dy_f ** 2 + dx_f ** 2
        #         if distance_sq < min_distance:
        #             min_distance = distance_sq
        #             closest_food = f

        #     if closest_food is not None:
        #         middle_x = (self.colony.x + closest_food.x) / 2
        #         middle_y = (self.colony.y + closest_food.y) / 2
        #         dx, dy = middle_x - self.x, middle_y - self.y
        #         dx, dy = dx / ((abs(dx) + abs(dy)) + x), dy / ((abs(dx) + abs(dy)) + x)

        
        return dx, dy, pass_food_to_agent

    def get_direction(self, destination):
        dx = destination.x - self.x
        dy = destination.y - self.y
        return dx, dy
        
    
    def reset(self):
        self.x = random.randint(0, SCREEN_SIZE)
        self.y = random.randint(0, SCREEN_SIZE)
        self.hit_wall = 0
        self.total_rewards = 0
        self.has_food = 0
        self.agent_passed_to = None
        self.steps_since_passing = 0
        
        
    def sense(self, foods):
        perceptions = np.empty((0, 2))

        food_coords = np.array([[food.x, food.y] for food in foods])
        dx_dy = food_coords - np.array([self.x, self.y])
        distances_squared = np.sum(dx_dy**2, axis=1)
        mask = distances_squared <= PERCEPTION_RADIUS**2

        perceptions = dx_dy[mask]
        sorted_indices = np.argsort(np.sum(perceptions**2, axis=1))
        perceptions = perceptions[sorted_indices][:self.max_objects]
  
        return perceptions
    
    def sense_closest_agents(self, agents):
        closest_agent = agents[0]
        closest_distance = self.distance(agents[0])
        for agent in agents:
            if agent == self:
                continue
            agent_distance = self.distance(agent)
            if agent_distance < closest_distance:
                closest_agent = agent
                closest_distance = agent_distance
        if closest_distance <= PERCEPTION_RADIUS:
            return closest_agent
        else:
            return None
    
    
    def distance(self, agent):
        return math.sqrt((self.x - agent.x)**2 + (self.y - agent.y)**2)

    
    def draw(self, screen):
        pygame.draw.circle(screen, self.color, (self.x, self.y), self.radius)
        
        if self.has_food:
            pygame.draw.circle(screen, (0, 0, 255), (self.x, self.y), AGENT_RADIUS // 2)
        
        if self.agent_passed_to != None:
            print("Drawing")
            value = min(255, max(20, int(255 - (self.steps_since_passing / 100))))
            line_color = (value, value, value)
            pygame.draw.line(screen, line_color, (self.x, self.y), (self.agent_passed_to.x, self.agent_passed_to.y))


    def collect_food(self, food_locations):
        for location in food_locations:
            distance = np.sqrt((self.x - location.x) ** 2 + (self.y - location.y) ** 2)
            if distance < self.radius + location.radius:
                location.collect()
                self.has_food = 1
                self.agents_involved = {self}  # reset the set of involved agents when picking up food
                return 0
        return 0

    def deposit_food(self):
        distance_to_colony = np.sqrt((self.x - self.colony.x) ** 2 + (self.y - self.colony.y) ** 2)
        if distance_to_colony < self.radius + self.colony.radius and self.has_food:
            self.has_food = 0
            return 1, self.agents_involved
        return 0, set()
        
            
class World:
    def __init__(self, agents, food_locations, brain, colony):
        self.agents = agents
        self.food_locations = food_locations
        self.num_agents = len(agents)
        self.brain = brain
        self.colony = colony
        
    def step(self, epsilon=0.0):
        total_reward = 0
        reward_agents = set()

        for agent in self.agents:
            reward, involved_agents = agent.deposit_food()
            if reward > 0:
                total_reward += reward
                reward_agents.update(involved_agents)
        
        # if total_reward > 0:
        #     print(f"reward agents: {len(reward_agents)}, reward: {total_reward}")
        for agent in reward_agents:
            agent.total_rewards += total_reward * len(reward_agents)

        for agent in self.agents:
            agent.step(self.food_locations, total_reward, self.agents, epsilon=epsilon)
    
            
    def update_agents_brains(self):
        for agent in self.agents:
            agent.brain.update()
            agent.brain.update_target_q_network()
                
                
    def draw(self, screen):
            
        for food in self.food_locations:
            food.draw(screen)
        
        self.colony.draw(screen)
                    
        for agent in self.agents:
            agent.draw(screen)
            
    def randomize_reset(self):
        rewards = []
        for agent in agents:
            rewards.append(agent.total_rewards)
            agent.reset()
            
        self.colony.randomize()
        for f in self.food_locations:
            f.reset_random()
            while math.sqrt((f.x - self.colony.x)**2 + (f.y - self.colony.y)**2) < (SCREEN_SIZE / 1.5):
                self.colony.randomize()
                f.reset_random()
                # print(f"colony: {self.colony.x, self.colony.y}, food: {f.x, f.y}")
                # print(f"{math.sqrt((f.x - self.colony.x)**2 + (f.y - self.colony.y)**2)} < {(SCREEN_SIZE / 10) ** 2}")
                # print("reset")
        
        return rewards
    
    
def create_agents_brain_colony(num_agents, agent_radius, batch_size):
    colony = Colony(random.randint(0, SCREEN_SIZE), random.randint(0, SCREEN_SIZE))
    agents = [Agent(random.randint(0, SCREEN_SIZE), random.randint(0, SCREEN_SIZE), agent_radius, brain=None, colony=colony) for _ in range(num_agents)]
    brain = Brain(input_size=agents[0].input_len, output_size=agents[0].output_len, hidden_size=64, batch_size=64, memory_size=10000, gamma=0.99, lr=1e-3, device=device)
    for a in agents:
        a.brain = brain
    return agents, brain, colony

def create_food_locations(num_locations, num_foods):
    food_locations = [FoodLocation(random.randint(0, SCREEN_SIZE), random.randint(0, SCREEN_SIZE)) for _ in range(num_locations)]
    return food_locations


def plot_avg_fitness(avg_fitness_top_25_percent):
    plt.plot(avg_fitness_top_25_percent)
    plt.xlabel('Generation')
    plt.ylabel('Average Fitness of Top 25%')
    plt.title('Evolution of Fitness over Generations')
    plt.show()


def main(world:World):
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_SIZE, SCREEN_SIZE))
    pygame.display.set_caption('Random Population Movement with Senses')
    clock = pygame.time.Clock()
    world.randomize_reset()

    running = True
    steps = 0
    with torch.no_grad():
        while running:
            steps += 1
            if steps % 500 == 0:
                world.randomize_reset()
            screen.fill((0, 0, 0))
            world.step(epsilon=0.1)
            world.draw(screen)
            pygame.display.flip()
            clock.tick(30)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

    pygame.quit()
    
    
def parse_args():
    parser = argparse.ArgumentParser(description="Random Population Movement with Senses")
    parser.add_argument("--num_agents", type=int, default=8, help="Number of agents in the simulation")
    parser.add_argument("--agent_radius", type=float, default=AGENT_RADIUS, help="Radius of the agents")
    parser.add_argument("--num_food_locations", type=int, default=1, help="Number of food locations")
    parser.add_argument("--num_foods", type=int, default=NUM_FOODS, help="Number of foods in each location")
    parser.add_argument("--speed", type=int, default=5, help="Number of foods in each location")
    # parser.add_argument("--screen_size", type=int, default=500, help="Size of the screen for the simulation display")
    parser.add_argument("--model_path", type=str, default=None, help="Path to the saved model")
    parser.add_argument("--save_path", type=str, default="./models/hive_coop_dumb.pt", help="Path for saving the model")
    parser.add_argument("--train", action="store_true", help="Train the model")
    parser.add_argument("--display", action="store_true", help="Display the simulation using pygame (can't if training)")
    parser.add_argument("--epochs", type=int, default=10, help="How many epochs to train for")
    parser.add_argument("--steps", type=int, default=10000, help="Steps per epoch")
    parser.add_argument("--display_steps", type=int, default=200, help="Steps before reset when displaying")

    parser.add_argument("--batch_size", type=int, default=64, help="Batch size")
    args = parser.parse_args()
    return args

def run_simulation(world, display):
    if display:
        main(world)
    else:
        train(world)

def train(world):
    print("Training...")
    epsilon = 1
    average = 0
    max_average = 0
    reset_interval = 1000
    epsilon_at_max_average = 0
    epsilon_base = 1
    epsilons = []
    averages = []
    for epoch in range(epochs):
        print("Epoch:", epoch)
        for step in range(ep_steps):
            world.step(epsilon=epsilon)
            if step % update_interval == 0:
                world.update_agents_brains()
            # reset_interval = (min(1000, (epoch + 1) * 200))
            if (step + 1) % reset_interval == 0:
                rewards = world.randomize_reset()
                average = (sum(rewards) / len(rewards)) * (1000 / reset_interval) # normalize averages to 1000 steps per reset
                averages.append(average)
                max_average = max(max_average, average)
                epsilon_at_max_average = epsilon
                epsilon = max(0.1, epsilon_base - (average * args.num_agents / NUM_FOODS))
                epsilons.append(epsilon)
                epsilon_base *= 0.999
            if (step + 1) % 10000 == 0:
                print(f"Avg rewards at epsilon = {epsilon}, epoch {epoch} step {step + 1}: {average}, max_average = {max_average} at epsilon {epsilon_at_max_average}")
                max_average = 0
        if epoch % 5 == 0:
            world.brain.save_model(args.save_path)  # Save the model
            print("Saved at epoch: ", epoch, "at", args.save_path)
        # main(world)
    plt.plot(averages)
    plt.xlabel("Resets")
    plt.ylabel("Average Reward per Reset")
    plt.title("Average Reward per Reset over Training")
    plt.show()
    plt.figure()
    plt.plot(epsilons)
    plt.xlabel("Resets")
    plt.ylabel("Epsilon")
    plt.title("Epsilon over Training")
    plt.show()
    main(world)
    

if __name__ == "__main__":
    args = parse_args()

    agents, brain, colony = create_agents_brain_colony(args.num_agents, args.agent_radius, args.batch_size)
    food_locations = create_food_locations(args.num_food_locations, args.num_foods)
    world = World(agents, food_locations, brain=brain, colony=colony) # top 15% multiply
    if args.model_path != None:
        brain.load_model(args.model_path)
        print(f"Loaded model from {args.model_path}")
    epochs = args.epochs
    ep_steps = args.steps
    save_interval = 500000000000
    avg_data = []
    update_interval = 10
    SPEED = args.speed

    if args.train:
        # world.brain.load_model(args.model_path)
        world.randomize_reset()
        for a in world.agents:
            a.brain = world.brain
        train(world)

    if args.display:
        # world.brain.load_model(args.model_path)
        world.randomize_reset()
        for a in world.agents:
            a.brain = world.brain

        run_simulation(world, args.display)