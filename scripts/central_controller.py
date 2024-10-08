#!/usr/bin/env python3.8
# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import rospy
import numpy as np
import re
import time
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from path_finder import PathFinder
from pmadmu_planner.msg import Formation, GoalPose, Trajectory, Trajectories, FollowerFeedback
from geometry_msgs.msg import Pose, PoseStamped
from dynamic_reconfigure.server import Server
from pmadmu_planner.cfg import PathFinderConfig


class CentralController:
    def __init__(self) -> None:
        rospy.init_node('CentralController')

        rospy.loginfo("[CController] Waiting for Services...")
        rospy.wait_for_service('/pmadmu_planner/pixel_to_world')
        rospy.wait_for_service('/pmadmu_planner/world_to_pixel')
        rospy.loginfo("[CController] transformation services are running!")

        robot_names : str = str(rospy.get_param('~robot_names'))
        self.unique_mir_ids : list[str] = robot_names.split(',')

        #? do we still need this? -> leader pose via rviz
        #leader_position : str = str(rospy.get_param('~leader_position'))
        #leader_pose : list[float] = ast.literal_eval(leader_position)
        #if len(leader_pose) != 3:
        #    rospy.logerr(f"leader_position is invalid. Must contain 3 Values for [x, y, rotation] but contains {len(leader_pose)}. Please adjust the launch file!")
        #    return None
        #rospy.loginfo(f"Received the leader pose at {leader_pose}")



        #rate : rospy.Rate = rospy.Rate(1.0)
        #while True:
        #    robot_name = rospy.get_param('/robot_name', 'this robot does not exist. your config doesnt work. idiot!')
        #    max_speed = rospy.get_param('/max_speed', -0.0)
        #    rospy.logerr(f"Robot Name: {robot_name}")
        #    rospy.logerr(f"Max Speed: {max_speed}")
        #    rate.sleep()


        self.path_finders : dict[str, PathFinder] = {name: PathFinder(robot_name=name, robot_id=index) for index, name in enumerate(self.unique_mir_ids)}
        #self.path_followers:dict[int, PathFollower] = {id: PathFollower(id) for id in self.unique_mir_ids}
        self.current_formation : Formation | None = None
        self.grid : np.ndarray | None = None
        self.cv_bridge : CvBridge = CvBridge()
        
        self.map_subscriber : rospy.Subscriber = rospy.Subscriber("/pmadmu_planner/map", Image, self.map_callback)
        self.follower_feedback_subscriber : rospy.Subscriber = rospy.Subscriber('/pmadmu_planner/follower_status', FollowerFeedback, self.follower_feedback)
        self.rviz_goal_subscriber : rospy.Subscriber = rospy.Subscriber("/pmadmu_planner/rviz_goal", PoseStamped, self.receive_goal_pos)

        self.trajectory_publisher : rospy.Publisher = rospy.Publisher('/pmadmu_planner/trajectories', Trajectories, queue_size=10, latch=True)
        self.formation_subscriber : rospy.Subscriber = rospy.Subscriber("/pmadmu_planner/formation", Formation, self.build_formation)

        # Send formation request; remove this if you want to trigger the planning process by external nodes
        self.formation_publisher : rospy.Publisher = rospy.Publisher("/pmadmu_planner/formation", Formation, queue_size=10, latch=True)
        config_server = Server(PathFinderConfig, self.config_change)
        return None
    

    def config_change(self, config, level):
        rospy.loginfo("[CController] Config was changed, updating path finders...")
        for path_finder in self.path_finders.values():
            path_finder.config_change(config, level)
        return config
    

    def receive_goal_pos(self, goal: PoseStamped) -> None:
        rospy.logerr(f"[CController] received a new goal pose at {goal.pose.position.x} {goal.pose.position.y}")

        robot_positions : str = str(rospy.get_param('~robot_positions'))
        robot_positions_list = ast.literal_eval(robot_positions)

        formation : Formation = Formation()
        formation.goal_poses = []

        if len(self.unique_mir_ids) != len(robot_positions_list):
            rospy.logerr(f"[CController] There must be the same number of robots ({len(self.unique_mir_ids)}) and positions ({len(robot_positions_list)}). Please adjust the launch file!")
            return None
        
        for index, robot_name in enumerate(self.unique_mir_ids):
            robot_position : list[float] = robot_positions_list[index]
            if len(robot_position) != 3:
                rospy.logerr(f"[CController] Position for {robot_name} is invalid. Must contain 3 Values for [x, y, rotation] but contains {len(robot_name)}. Please adjust the launch file!")
                return None
            goal_pose : GoalPose = GoalPose()
            goal_pose.robot_name = robot_name
            pose : Pose = Pose()
            pose.position.x = goal.pose.position.x + robot_position[0] * np.cos(goal.pose.orientation.z) - robot_position[1] * np.sin(goal.pose.orientation.z)
            pose.position.y = goal.pose.position.y + robot_position[0] * np.sin(goal.pose.orientation.z) + robot_position[1] * np.cos(goal.pose.orientation.z)
            pose.position.z = 0.0
            pose.orientation.z = goal.pose.orientation.z + robot_position[2]
            goal_pose.goal = pose
            goal_pose.priority = index
            formation.goal_poses.append(goal_pose)
            rospy.loginfo(f"[CController] {robot_name} received a goal position. relative: {robot_position} -> absolute: [{pose.position.x}, {pose.position.y}]; angle {pose.orientation.z}")
        self.formation_publisher.publish(formation)
        return None
    

    def follower_feedback(self, feedback : FollowerFeedback) -> None:
        if self.current_formation is None:
            return None
        if feedback.status == feedback.LOST_WAYPOINT:
            rospy.loginfo(f"[CController] Replanning because Robot {feedback.robot_id} lost its waypoint.")
            self.build_formation(self.current_formation)
        if feedback.status == feedback.OUTSIDE_RESERVED_AREA:
            rospy.loginfo(f"[CController] Replanning because Robot {feedback.robot_id} left its reserved area.")
            self.build_formation(self.current_formation)
        if feedback.status == feedback.PATH_BLOCKED:
            rospy.loginfo(f"[CController] Replanning because Robot {feedback.robot_id} Path is blocked.")
            self.build_formation(self.current_formation)
        return None
    
    def check_priorities(self) -> bool:
        if self.current_formation is None or self.current_formation.goal_poses is None:
            rospy.logerr(f"[CController] Can't check priorities since goal poses or current formation are None")
            return False
        prio_counter : dict[int, int] = {}
        for goal_pose in self.current_formation.goal_poses:
            if goal_pose.priority is None:
                rospy.loginfo(f"[CController] Priority of Robot {goal_pose.robot_name} is None. Will reset priorities")
                return False
            prio_counter[goal_pose.priority] = prio_counter.get(goal_pose.priority, 0) + 1

        for prio, count in prio_counter.items():
            if count != 1:
                rospy.logwarn(f"[CController] Priority {prio} is assigned to multiple ({count}) robots. Will reset priorities")
                return False
        return True

    def initialize_priorities(self, priorities : list[int] = []) -> None:
        if self.current_formation is None or self.current_formation.goal_poses is None:
            rospy.logwarn(f"[CController] Can't initialize priorities since goal poses or current formation are None")
            return None
        if len(priorities) == len(self.current_formation.goal_poses):
            for index, prio in enumerate(priorities):
                if index >= len(self.current_formation.goal_poses):
                    break
                self.current_formation.goal_poses[index].priority = prio
            return None
        rospy.loginfo("[CController] No or invalid ammount of Priorities given, using default values instead.")
        for index, goal_pose in enumerate(self.current_formation.goal_poses):
            goal_pose.priority = index + 1
        return None
    
    
    def reassign_priorities(self, robot_name : str) -> bool:
        if self.current_formation is None or self.current_formation.goal_poses is None:
            rospy.logerr(f"[CController] Can't change Priority of Robot {robot_name} since goal poses or current formation are None")
            return False
        # set failed planner to highest priority
        prio_failed_robot : int = -1
        for goal_pose in self.current_formation.goal_poses:
            if goal_pose.robot_name == robot_name:
                prio_failed_robot = goal_pose.priority
                goal_pose.priority = 1
                break
        if prio_failed_robot == -1:
            rospy.logerr(f"[CController] Can't change Priority of Robot {robot_name} since planner id seems to not exist in the current formation.")
            return False
        # adjust priorities of remaining robots
        for goal_pose in self.current_formation.goal_poses:
            if goal_pose.robot_name != robot_name and goal_pose.priority < prio_failed_robot:
                goal_pose.priority += 1
        return True


    def build_formation(self, formation : Formation) -> None:

        wait_time : float = time.time()
        while self.grid is None and not rospy.is_shutdown():
            rospy.loginfo("[CController] Planner waiting for map data...")
            rate : rospy.Rate = rospy.Rate(1)
            rate.sleep()
            if time.time() - wait_time > 10.0:
                break
        
        if self.grid is None:
            rospy.logwarn("[CController] Failed to plan paths since there is no map data...")
            return None
        
        if formation.goal_poses is None:
            rospy.logwarn("[CController] Received an empty formation request.")
            return None
        self.current_formation = formation
        start_time : float = time.time()
        rospy.loginfo(f"[CController] Received a planning request for {len(formation.goal_poses)} robots.")

        while not self.check_priorities():
            self.initialize_priorities()
        else:
            rospy.loginfo("[CController] Priorities are set correctly.")
        
        planned_trajectories : list[Trajectory] = []
        failed_planner : str | None = None
        formation.goal_poses.sort(key=lambda x: x.priority)
        for gp in formation.goal_poses:
            goal_pose : GoalPose = gp
            if goal_pose.robot_name not in self.path_finders.keys():
                rospy.logwarn(f"[CController] Received a request for robot {goal_pose.robot_name} but this robot seems to not exist.")
                continue
            if goal_pose.goal is None:
                rospy.logwarn(f"[CController] Received a request for robot {goal_pose.robot_name} the goal position is none.")
                continue
            planned_trajectory : Trajectory | None = self.path_finders[goal_pose.robot_name].search_path(self.grid, goal_pose, planned_trajectories)
            if planned_trajectory is not None:
                planned_trajectories.append(planned_trajectory)
            else:
                failed_planner = goal_pose.robot_name
                rospy.logwarn(f"[CController] Planner failed {failed_planner}")
                break

        

        if failed_planner is not None:
            rospy.logwarn(f"[CController] ------------ Planning failed! ({time.time()-start_time:.3f}s) ------------ ")
            for goal_pose in formation.goal_poses:
                if goal_pose.robot_name == failed_planner:
                    if goal_pose.priority == 1:
                        rospy.logerr(f"[CController] Planner {failed_planner} failed with highest priority. There is no valid solution.")
                        return None
                    break
            if self.reassign_priorities(goal_pose.robot_name):
                rospy.loginfo("[CController] Retrying with reordered priorites...")
                self.build_formation(self.current_formation)
            else:
                rospy.logerr(f"[CController] Failed to replan with reordered priorities.")
            return None

        rospy.loginfo(f"[CController] ------------ Planning done! ({time.time()-start_time:.3f}s) ------------ ")
        trajectories : Trajectories = Trajectories()
        trajectories.trajectories = [trajectory for trajectory in planned_trajectories]
        trajectories.timestamp = time.time()
        self.trajectory_publisher.publish(trajectories)

        #fb_visualizer.show_live_path(planned_trajectories)
        end_time = time.time()
        rospy.loginfo(f"[CController] ################## Done! ({end_time-start_time:.3f}s) ################## ")

        return None
    
    
    def map_to_grid(self, map : Image) -> np.ndarray:
        img = self.cv_bridge.imgmsg_to_cv2(map, desired_encoding="mono8")
        grid : np.ndarray = np.array(img) // 255
        return grid
    

    def map_callback(self, img_msg : Image) -> None:
        grid : np.ndarray = self.map_to_grid(img_msg)
        #todo: check if obstacles interfere with planned routes; replan if necessary
        self.grid = grid
        return None


    
    def generate_formation_position(self, robot_name : str, goal : tuple[float, float, float], size : float = 1.0) -> GoalPose:
        rospy.loginfo(f"[CController] Generating request for robot {id}")
        request : GoalPose = GoalPose()
        request.robot_name = robot_name
        request.goal = Pose()
        request.goal.position.x, request.goal.position.y = goal[0], goal[1]
        request.goal.orientation.z = goal[2]
        request.robot_size = size
        return request
    



if __name__ == '__main__':
    central_controller: CentralController = CentralController()

    test_formation : Formation = Formation()
    test_formation.goal_poses = []
    east : float = 0.0
    west : float = np.pi
    north : float = np.pi/2
    south : float = -np.pi/2

    #goal_positions : list[tuple[float, float]] = [(50, 20), (50, 19), (30, 18), (51.1, 20)]

    formation_square = [(36, 37, north), (33, 39.5, north) , (36, 39.5, north), (33, 37, north)]
    formation_line_north = [(31, 38, north), (33.5, 38, north), (36, 38, north), (38.5, 38, north)]
    formation_line_north_scrambeled = [(33.5, 38, north),(38.5, 38, north), (36, 38, north), (31, 38, north)]
    formation_line_north_reversed = [ (38.5, 38, north), (36, 38, north),(33.5, 38, north), (31, 38, north)]
    formation_line_south = [(31, 34, north), (33.5, 34, north), (36, 34, north), (38.5, 34, north)]

    formation_line_ten_robots : list[tuple[float, float, float]] = [(53, 53, east),(53, 50, east),(53, 47, east),(53, 44, east)]#[(53, 51, east),(53, 49, east),(53, 47, east),(53, 45, east),(53, 43, east),(53, 41, north),(53, 39, north),(53, 37, north),(53, 35, north),(53, 33, north),]
    formation_scrambled_ten_robots = []
    goal_positions : list[tuple[float, float, float]] = formation_line_ten_robots

    #for index, goal_position in enumerate(goal_positions):
    #    test_formation.goal_poses.append(central_controller.generate_formation_position(index+1, goal_position, size=1.0))

    rate : rospy.Rate = rospy.Rate(1)
    rate.sleep()

    #temp_pub : rospy.Publisher = rospy.Publisher("/pmadmu_planner/formation", Formation, queue_size=10, latch=True)
    #temp_pub.publish(test_formation)

    rospy.spin()